from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from core.application.tooling.quality import sonar_size_precheck as precheck
from core.application.tooling.quality.check_registry import load_check_registry

ROOT = Path(__file__).resolve().parents[4]
LINT_SCRIPT = ROOT / "core/entrypoints/cli/scripts/lint.sh"
MAXIMUM = 10


# --------------------------------------------------------------------------- #
# Counting -- each rule matches the number in Sonar's own issue message
# --------------------------------------------------------------------------- #


def test_python_count_is_line_terminators_plus_one_like_the_eof_token() -> None:
    # Sonar reported 1,001 lines for a 1000-line module ending in a newline.
    assert precheck.count_python_lines("a = 1\n" * 1000) == 1001
    assert precheck.count_python_lines("a = 1\n" * 999 + "a = 1") == 1000
    assert precheck.count_python_lines("a\r\nb\rc\n") == 4
    assert precheck.count_python_lines("") == 1


def test_js_count_skips_blank_and_comment_only_lines() -> None:
    source = "\n".join(
        [
            "// leading comment",
            "const a = 1; // trailing comment",
            "",
            "/*",
            " * block",
            " */",
            "/** doc */ const b = 2;",
            "   ",
            "export { a, b };",
        ]
    )
    assert precheck.count_js_lines_of_code(source) == 3


def test_js_multiline_tokens_count_every_spanned_line() -> None:
    source = "const t = `first\n\n${a + `${b}`}\nlast`;\nconst s = 'x\\\ny';\n"
    # template spans 1-4 including the blank line; the continued string spans 5-6
    assert precheck.count_js_lines_of_code(source) == 6


def test_js_comment_markers_inside_strings_and_regex_are_code() -> None:
    source = "\n".join(
        [
            "const url = 'https://example.test/*';",
            "const re = /\\/*/g;",
            "next();",
            "const q = a / b / c;",
            "after();",
        ]
    )
    assert precheck.count_js_lines_of_code(source) == 5


def test_js_jsx_text_apostrophe_does_not_swallow_following_lines() -> None:
    source = "\n".join(
        [
            "export const X = () => (",
            "  <p>Don't stop</p>",
            ");",
            "// only a comment",
        ]
    )
    assert precheck.count_js_lines_of_code(source) == 3


def test_js_shebang_is_not_code() -> None:
    assert precheck.count_js_lines_of_code("#!/usr/bin/env node\nrun();\n") == 1


# --------------------------------------------------------------------------- #
# Properties and patterns
# --------------------------------------------------------------------------- #


def test_properties_parser_handles_comments_and_continuations() -> None:
    values = precheck.parse_properties(
        "# comment\n! also comment\nsonar.sources=core,\\\n    assets\nkey : value\n"
    )
    assert values == {"sonar.sources": "core,assets", "key": "value"}


def test_sonar_wildcards() -> None:
    nested = precheck.compile_pattern("**/node_modules/**")
    assert nested.match("node_modules/x.js")
    assert nested.match("assets/frontend/node_modules/a/b.js")
    tests = precheck.compile_pattern("assets/frontend/src/**/*.test.ts")
    assert tests.match("assets/frontend/src/a.test.ts")
    assert tests.match("assets/frontend/src/a/b/c.test.ts")
    assert not tests.match("assets/frontend/src/a.test.tsx")
    single = precheck.compile_pattern("core/*.py")
    assert single.match("core/a.py")
    assert not single.match("core/a/b.py")


# --------------------------------------------------------------------------- #
# Selection and baseline on a temporary repository
# --------------------------------------------------------------------------- #


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "infra/tooling/sonar-project.properties",
        "sonar.sources=assets\nsonar.exclusions=**/node_modules/**,assets/**/*.test.ts,**/*.py\n",
    )
    _write(
        tmp_path,
        "infra/tooling/sonar-project.py.properties",
        "sonar.sources=core\nsonar.tests=tests\nsonar.python.file.suffixes=.py\n"
        "sonar.exclusions=**/*.js\n",
    )
    long_py = "x = 1\n" * MAXIMUM  # MAXIMUM + 1 by Sonar's count
    long_ts = "f();\n" * (MAXIMUM + 1)
    _write(tmp_path, "core/big.py", long_py)
    _write(tmp_path, "core/fits.py", "x = 1\n" * (MAXIMUM - 1))
    _write(tmp_path, "core/legacy.js", long_ts)  # excluded by the py profile
    _write(tmp_path, "tests/test_huge.py", long_py * 5)  # test files: no S104
    _write(tmp_path, "assets/app.ts", long_ts)
    _write(tmp_path, "assets/commented.ts", "f();\n" + "// note\n" * 50)
    _write(tmp_path, "assets/app.test.ts", long_ts)
    _write(tmp_path, "assets/node_modules/pkg/index.js", long_ts)
    _write(tmp_path, "assets/upload/dist/bundle.js", long_ts)  # SonarJS default exclusion
    _write(tmp_path, "assets/notes.py", long_py)  # excluded by the stable profile
    return tmp_path


def _baseline(root: Path, files: list[str]) -> Path:
    path = root / precheck.BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"maximum": MAXIMUM, "files": files}), encoding="utf-8")
    return path


def _run(root: Path, **kwargs: Any) -> tuple[int, str]:
    out = io.StringIO()
    code = precheck.run_sonar_size_precheck(root, out=out, **kwargs)
    return code, out.getvalue()


def test_selection_follows_sources_tests_and_exclusions(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    selected = dict(precheck.iter_selected_files(root, precheck.load_profiles(root)))
    assert selected == {
        "assets/app.ts": "ts",
        "assets/commented.ts": "ts",
        "core/big.py": "py",
        "core/fits.py": "py",
    }


def test_new_over_limit_files_fail(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _baseline(root, [])
    code, output = _run(root)
    assert code == 1
    assert f"NEW    assets/app.ts: typescript:S104 {MAXIMUM + 1} lines" in output
    assert f"NEW    core/big.py: python:S104 {MAXIMUM + 1} lines" in output
    assert "fits.py" not in output
    assert "commented.ts" not in output


def test_baseline_files_are_reported_not_failed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _baseline(root, ["assets/app.ts", "core/big.py"])
    code, output = _run(root)
    assert code == 0
    assert "known  core/big.py" in output
    assert "new=0 stale=0" in output
    assert _run(root, use_baseline=False)[0] == 1


def test_stale_baseline_entries_fail_so_the_baseline_only_shrinks(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _baseline(root, ["assets/app.ts", "core/big.py", "core/fits.py", "core/deleted.py"])
    code, output = _run(root)
    assert code == 1
    assert "STALE  core/fits.py" in output
    assert "STALE  core/deleted.py" in output


def test_profile_run_judges_only_its_own_baseline_entries(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _baseline(root, ["assets/app.ts", "core/big.py", "core/deleted.py"])
    code, output = _run(root, profiles=["py"])
    assert code == 0, output
    assert "profiles=py " in output
    assert "assets/app.ts" not in output


def test_update_baseline_records_current_findings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    path = _baseline(root, [])
    assert _run(root, update_baseline=True)[0] == 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["files"] == ["assets/app.ts", "core/big.py"]
    assert payload["maximum"] == MAXIMUM
    assert _run(root)[0] == 0


def test_missing_profiles_is_a_configuration_error(tmp_path: Path) -> None:
    assert _run(tmp_path)[0] == 2


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_precheck_covers_exactly_the_rules_the_repository_activates() -> None:
    from core.application.tooling.quality.sonar_ensure_rules import REQUIRED_RULES

    assert set(precheck.RULE_BY_LANGUAGE.values()) == set(REQUIRED_RULES)


def test_registry_owns_the_precheck_in_tier_two() -> None:
    registry = load_check_registry(ROOT / "infra/tooling/quality/check-registry.json")
    check = next(item for item in registry.checks if item.id == "sonar-size")
    assert check.owner_tier == 2
    assert check.command[-2:] == ("--checks", "sonar-size")
    assert precheck.BASELINE_PATH in check.inputs


def test_sonar_scan_runs_the_precheck_before_starting_sonarqube() -> None:
    script = LINT_SCRIPT.read_text(encoding="utf-8")
    body = script.split("run_host_sonar_scan() {", 1)[1].split("\n}\n", 1)[0]
    precheck_at = body.index("core.application.tooling.quality.sonar_size_precheck")
    assert precheck_at < body.index("up -d")
    assert precheck_at < body.index("sonar-scanner sonar-scanner")
    assert "return 1" in body[precheck_at : body.index("up -d")]
