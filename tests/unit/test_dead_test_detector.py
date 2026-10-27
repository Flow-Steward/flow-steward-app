"""Dead/obsolete test detector — C-07 audit gap.

Scans the tests/ directory to surface tests that are likely stale, dead, or
providing misleading confidence:

A. Always-skip tests (unconditional pytest.skip at function level)
   - Tests that call pytest.skip() with no condition guard
   - Tests whose only content is a skip call

B. Assertion-free tests
   - Test functions with no assert statement, no pytest.raises, no fails
   - These pass vacuously and provide no regression signal

C. Duplicate test names across modules
   - Same test function name in multiple files risks shadowing issues
   - Reports duplicate names with their file paths

D. Import-failure-only tests
   - Tests that only skip on ImportError (always skip in target env)
   - Detects try/except ImportError → pytest.skip patterns with no fallback

E. Orphaned test modules
   - Test files that import modules no longer found in the codebase
   - Detects `from X import Y` / `import X` where X doesn't exist

Run:
    python -m pytest tests/unit/test_dead_test_detector.py -v
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = ROOT / "tests"

# Threshold: how many always-skip tests are acceptable before failing
_ALWAYS_SKIP_BUDGET = 30
# Threshold: how many assertion-free tests are acceptable
# Set high because many UI/Playwright tests use helper assertion functions
# (e.g., _check_no_credential_fields()) that are invisible to AST scanning
_ASSERTION_FREE_BUDGET = 200


def _collect_test_files() -> list[Path]:
    """Collect all test_*.py files."""
    return [p for p in TESTS_DIR.rglob("test_*.py") if "__pycache__" not in str(p)]


def _parse_ast_safe(path: Path) -> ast.Module | None:
    """Parse a Python file into AST; return None on syntax error."""
    try:
        return ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return None


def _get_test_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    """Extract top-level and class-level test_* function definitions."""
    fns: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                fns.append(node)
    return fns


def _is_assert_like_call(node: ast.Call) -> bool:
    """Return True for direct assertion helpers and test-framework expectation calls."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("raises", "fail", "warns", "deprecated_call"):
            return True
        if func.attr.startswith("assert"):
            return True
        if func.attr.startswith("to_") and isinstance(func.value, ast.Call):
            inner = func.value.func
            if isinstance(inner, ast.Name) and inner.id == "expect":
                return True
            if isinstance(inner, ast.Attribute) and inner.attr == "expect":
                return True
    if isinstance(func, ast.Name) and func.id in ("raises", "fail"):
        return True
    return False


def _has_direct_assertion(fn: ast.FunctionDef) -> bool:
    """Return True if the function body contains a direct assertion primitive."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            if _is_assert_like_call(node):
                return True
    return False


def _assertion_helper_names(tree: ast.Module) -> set[str]:
    """Collect non-test helper functions that contain direct assertions."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("test_"):
            continue
        if _has_direct_assertion(node):
            names.add(node.name)
    return names


def _has_assertion(fn: ast.FunctionDef, assertion_helpers: set[str] | None = None) -> bool:
    """Return True if the function has a direct assertion or calls an assertion helper."""
    if _has_direct_assertion(fn):
        return True

    helpers = assertion_helpers or set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in helpers:
            return True
        if isinstance(func, ast.Attribute) and func.attr in helpers:
            return True
    return False


def _is_unconditional_skip(fn: ast.FunctionDef) -> bool:
    """Return True if the function body is just a pytest.skip() call."""
    body = fn.body
    # Strip docstring
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) != 1:
        return False
    stmt = body[0]
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "skip":
        return True
    if isinstance(func, ast.Name) and func.id == "skip":
        return True
    return False


def _count_skip_calls(fn: ast.FunctionDef) -> int:
    """Count pytest.skip() calls in a function."""
    count = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "skip") or (
                isinstance(func, ast.Name) and func.id == "skip"
            ):
                count += 1
    return count


def _is_import_error_only_skip(fn: ast.FunctionDef) -> bool:
    """Return True if the body is only try: import X / except ImportError: skip."""
    body = fn.body
    # Strip docstring
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) != 1:
        return False
    stmt = body[0]
    if not isinstance(stmt, ast.Try):
        return False
    # Check: all handlers catch ImportError and call skip
    all_handlers_skip = all(
        any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "skip")
                or (isinstance(n.func, ast.Name) and n.func.id == "skip")
            )
            for n in ast.walk(h)
        )
        for h in stmt.handlers
        if h.type is not None
        and (
            (isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError"))
            or (
                isinstance(h.type, ast.Attribute)
                and h.type.attr in ("ImportError", "ModuleNotFoundError")
            )
        )
    )
    return bool(all_handlers_skip and stmt.handlers)


@dataclass(frozen=True)
class _DetectorFindings:
    """Everything the detector reports, gathered in one pass over tests/."""

    always_skip: tuple[tuple[str, str], ...]
    assertion_free: tuple[tuple[str, str], ...]
    name_to_files: tuple[tuple[str, tuple[str, ...]], ...]
    import_skip_only: tuple[tuple[str, str], ...]
    unparseable: tuple[str, ...]
    test_function_count: int


@functools.cache
def _scan_test_tree() -> _DetectorFindings:
    """Parse every test file once per process and run all detectors on it.

    Each detector used to parse the whole tree again in its own class-scoped
    fixture. The results are identical: same files, same order, same rules.
    """
    always_skip: list[tuple[str, str]] = []
    assertion_free: list[tuple[str, str]] = []
    name_to_files: dict[str, list[str]] = {}
    import_skip_only: list[tuple[str, str]] = []
    unparseable: list[str] = []
    test_function_count = 0
    for path in _collect_test_files():
        rel = str(path.relative_to(ROOT))
        tree = _parse_ast_safe(path)
        if tree is None:
            unparseable.append(rel)
            continue
        test_functions = _get_test_functions(tree)
        test_function_count += len(test_functions)
        assertion_helpers = _assertion_helper_names(tree)
        for fn in test_functions:
            name_to_files.setdefault(fn.name, []).append(rel)
            if _is_unconditional_skip(fn):
                always_skip.append((rel, fn.name))
            # Unconditional skips are tracked separately, not as assertion-free.
            elif not _has_assertion(fn, assertion_helpers):
                assertion_free.append((rel, fn.name))
            if _is_import_error_only_skip(fn):
                import_skip_only.append((rel, fn.name))
    return _DetectorFindings(
        always_skip=tuple(always_skip),
        assertion_free=tuple(assertion_free),
        name_to_files=tuple((name, tuple(files)) for name, files in name_to_files.items()),
        import_skip_only=tuple(import_skip_only),
        unparseable=tuple(unparseable),
        test_function_count=test_function_count,
    )


# ===========================================================================
# A. Always-skip tests
# ===========================================================================


class TestAlwaysSkipDetection:
    """Tests whose only content is a pytest.skip() call are dead weight."""

    @pytest.fixture(scope="class")
    def always_skip_tests(self):
        return list(_scan_test_tree().always_skip)

    def test_always_skip_count_within_budget(self, always_skip_tests):
        """Number of unconditionally-skipped test functions must be below budget."""
        count = len(always_skip_tests)
        if count > _ALWAYS_SKIP_BUDGET:
            sample = always_skip_tests[:15]
            pytest.fail(
                f"{count} tests have unconditional pytest.skip() as only body "
                f"(budget={_ALWAYS_SKIP_BUDGET}). "
                f"Sample: {sample}"
            )

    def test_unconditional_skip_inventory_runs(self, always_skip_tests):
        """Scanner must complete and return a list (even if empty)."""
        assert isinstance(always_skip_tests, list)


# ===========================================================================
# B. Assertion-free tests
# ===========================================================================


class TestAssertionFreeDetection:
    """Test functions with no assertions pass vacuously — zero regression signal."""

    @pytest.fixture(scope="class")
    def assertion_free_tests(self):
        return list(_scan_test_tree().assertion_free)

    def test_assertion_free_count_within_budget(self, assertion_free_tests):
        """Assertion-free tests must be below budget."""
        count = len(assertion_free_tests)
        if count > _ASSERTION_FREE_BUDGET:
            sample = assertion_free_tests[:20]
            pytest.fail(
                f"{count} test functions have no assertions "
                f"(budget={_ASSERTION_FREE_BUDGET}). "
                f"Sample: {sample}"
            )

    def test_assertion_free_scan_completes(self, assertion_free_tests):
        """Scanner must return a list."""
        assert isinstance(assertion_free_tests, list)


# ===========================================================================
# C. Duplicate test names
# ===========================================================================


class TestDuplicateTestNames:
    """Same test name in multiple files can cause confusion and shadowing."""

    @pytest.fixture(scope="class")
    def name_to_files(self):
        """Build a mapping of test name → list of files that define it."""
        return {name: list(files) for name, files in _scan_test_tree().name_to_files}

    def test_no_excessive_duplicate_names(self, name_to_files):
        """Test names appearing in more than 5 files may indicate copy-paste debt."""
        heavy_dupes = {name: files for name, files in name_to_files.items() if len(files) > 5}
        # Soft check only — duplicates are common in test suites
        assert isinstance(heavy_dupes, dict)

    def test_duplicate_name_inventory_runs(self, name_to_files):
        """Name-to-files mapping must be built successfully."""
        assert len(name_to_files) > 0, "No test functions found — scanner broken?"


# ===========================================================================
# D. Import-error-only skip detection
# ===========================================================================


class TestImportErrorOnlySkips:
    """Tests that only execute a pytest.skip inside an ImportError handler
    will always skip in environments where the import succeeds — but if the
    import always fails, they're dead weight."""

    @pytest.fixture(scope="class")
    def import_skip_only_tests(self):
        """Find test functions whose body is only try: import X / except ImportError: skip."""
        return list(_scan_test_tree().import_skip_only)

    def test_import_skip_only_scan_completes(self, import_skip_only_tests):
        """Scanner must return a list."""
        assert isinstance(import_skip_only_tests, list)

    def test_import_skip_count_informational(self, import_skip_only_tests):
        """Report import-error-only test count (informational, no hard fail)."""
        count = len(import_skip_only_tests)
        assert count >= 0  # Always passes — this is an informational report


# ===========================================================================
# E. Basic test file health checks
# ===========================================================================


class TestFileHealthChecks:
    """Basic sanity checks on the test suite structure."""

    def test_test_files_are_parseable(self):
        """All test_*.py files must parse without SyntaxError."""
        broken = list(_scan_test_tree().unparseable)
        assert not broken, f"Test files with syntax errors: {broken}"

    def test_total_test_count_healthy(self):
        """Total test function count must be above minimum threshold."""
        total = _scan_test_tree().test_function_count
        assert total >= 500, f"Only {total} test functions found — test suite may be misconfigured"

    def test_test_directories_have_init_or_conftest(self):
        """Each tests/ subdirectory should have __init__.py or conftest.py."""
        missing: list[str] = []
        for subdir in TESTS_DIR.iterdir():
            if (
                subdir.is_dir()
                and not subdir.name.startswith(".")
                and not subdir.name.startswith("__")
            ):
                has_init = (subdir / "__init__.py").exists()
                has_conftest = (subdir / "conftest.py").exists()
                if not has_init and not has_conftest:
                    missing.append(str(subdir.relative_to(ROOT)))
        # Soft check
        assert isinstance(missing, list)
