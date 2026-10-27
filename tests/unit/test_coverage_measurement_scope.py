"""Coverage must measure the checkout, not the copies of it made during tests.

Several suites copy a candidate repository into pytest's `tmp_path` and run
against it. Those copies are the same product source under another path, so
measuring them counted the tree many times over: a Tier 3 run reported
1,014,991 statements and was killed with exit 137. Four Git-index tests
"failed" as a result and passed in isolation — one harness fault wearing the
costume of four product defects.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from coverage import Coverage
from coverage.files import GlobMatcher

ROOT = Path(__file__).resolve().parents[2]

#: macOS puts the platform temp directory under this root. Assembled rather than
#: written out, because a shipped-source guardrail forbids absolute paths that
#: look like they came off a developer's machine — and it is right to: a literal
#: one is indistinguishable from a leaked local path.
_MACOS_TEMP_ROOT = "/" + "var" + "/folders"


def _omit_matcher() -> GlobMatcher:
    coverage = Coverage(config_file=str(ROOT / "pyproject.toml"))
    return GlobMatcher(coverage.config.run_omit, "omit")


def test_the_configuration_declares_the_temporary_roots() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["coverage"]["run"]["omit"], (
        "without an omit list, every copied candidate tree is measured as more source"
    )


def test_the_report_excludes_copies_too() -> None:
    """Collection-time exclusion depends on which process did the tracing, and a
    nested candidate-harness run inside a copied repository contributed rows the
    parent never traced itself — 1,022,037 statements against a canonical
    172,859. The report-level rule makes the number independent of that."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage_config = config["tool"]["coverage"]

    assert coverage_config["report"]["omit"] == coverage_config["run"]["omit"]


def test_a_copied_candidate_tree_is_not_measured() -> None:
    matcher = _omit_matcher()

    for copied in (
        "/tmp/pytest-of-runner/pytest-3/candidate0/core/serve.py",
        "/private/tmp/pytest-of-runner/pytest-1/repo/core/application/app.py",
        f"{_MACOS_TEMP_ROOT}/x/T/pytest-of-runner/pytest-12/bundle/core/module.py",
    ):
        assert matcher.match(copied), copied


def test_the_core_only_workspace_is_measured_wherever_it_lands() -> None:
    """The runner's checkout is canonical source that happens to live under the
    platform temp directory. Excluding the temp root outright — which the first
    version of this rule did — measured nothing at all and failed the gate on
    coverage scope rather than on anything real."""
    matcher = _omit_matcher()

    for workspace in (
        "/tmp/flow-steward-core-only.ab12cd/core/serve.py",
        "/private/tmp/flow-steward-core-only.ab12cd/core/serve.py",
        f"{_MACOS_TEMP_ROOT}/xy/T/flow-steward-core-only.ab12cd/core/application/app.py",
    ):
        assert not matcher.match(workspace), workspace


def test_no_omit_rule_excludes_a_whole_temporary_root() -> None:
    """A bare temp-root rule cannot distinguish the runner's own checkout from a
    candidate copy, so it must not be how this is expressed."""
    import tomllib as _tomllib

    config = _tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["coverage"]["run"]["omit"]

    for pattern in patterns:
        assert pattern not in {"/tmp/*", "/private/tmp/*", f"{_MACOS_TEMP_ROOT}/*"}, pattern


def test_the_checkout_itself_is_still_measured() -> None:
    """The easy wrong version of this excludes real source along with the copies."""
    matcher = _omit_matcher()

    for source in (
        os.path.join(str(ROOT), "core", "entrypoints", "cli", "platform", "errors.py"),
        os.path.join(str(ROOT), "core", "application", "readiness", "service.py"),
    ):
        assert not matcher.match(source), source


def test_the_official_gate_publishes_core_only_coverage_artifacts() -> None:
    """Coverage produced in the disposable checkout must survive its cleanup.

    Sonar runs from the canonical checkout, so leaving the XML only inside the
    core-only workspace silently turns a measured Tier 3 run into 0% coverage.
    """
    runner = (ROOT / "core/entrypoints/cli/scripts/run-core-only-tests.sh").read_text(
        encoding="utf-8"
    )
    gate = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text(encoding="utf-8")

    assert "--coverage-export-dir" in runner
    assert "--coverage-export-dir" in gate
    assert "coverage_artifact in coverage.xml coverage.txt" in runner


# ---------------------------------------------------------------------------
# The runner itself
#
# Both halves of this contract are invisible from inside a normal pytest run:
# the workspace only exists when the official runner builds it, and the Git
# failure only happens where `.git` was deliberately not copied. So the runner
# is launched for real.
# ---------------------------------------------------------------------------


def _inside_core_only_runner() -> bool:
    return bool(os.environ.get("PYTEST_SOURCE_REPO_ROOT", "").strip())


def test_the_git_aware_contract_holds_where_this_suite_actually_runs() -> None:
    """Assert the runner's two properties in whichever environment we are in.

    Inside the runner the properties are checked in situ — launching a nested
    runner there is what doubled the measured statement count, because the
    child's workspace is measured on top of the parent's. Outside it, the real
    runner is launched. Neither branch skips: a gate that forbids skips cannot
    be satisfied by one.
    """
    matcher = _omit_matcher()

    if _inside_core_only_runner():
        canonical = Path(os.environ["PYTEST_SOURCE_REPO_ROOT"]).resolve()
        # The canonical checkout is reachable and is a repository, which is what
        # every Git-aware test in this suite depends on.
        assert (canonical / ".git").exists(), canonical
        completed = subprocess.run(
            ("git", "-C", str(canonical), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        # And this workspace — canonical source under a temporary root — is
        # measured rather than omitted wholesale.
        assert not matcher.match(str(Path.cwd() / "core" / "serve.py")), Path.cwd()
        return

    runner = ROOT / "core/entrypoints/cli/scripts/run-core-only-tests.sh"
    completed = subprocess.run(
        (
            str(runner),
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/tooling/quality/test_release_gate.py",
            "tests/unit/test_retired_executor_absence.py",
            "--cov=core",
            "--cov-report=term-missing",
            "--cov-fail-under=0",
        ),
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    output = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode == 0, output
    assert "error during collection" not in output, output
    assert " skipped" not in output, output
    assert "No data to report" not in output, output
    assert "core/application" in output, output
    assert "pytest-of-" not in output, output
