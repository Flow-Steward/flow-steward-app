#!/usr/bin/env python3
"""Predict SonarQube's file-size findings (S104) in about a second.

The local Sonar scan takes 40+ minutes; the size rule it enforces needs only the
file list and a line count. This module applies the same selection and the same
counting so an over-long file is reported before the scan starts.

Rules and thresholds (source of truth):

* ``python:S104``, ``javascript:S104`` and ``typescript:S104`` are the only size
  rules the repository activates (``sonar_ensure_rules.REQUIRED_RULES``). They are
  activated without parameters, so each runs with its default ``maximum`` of 1000;
  past scanner logs record "python:S104 active maximum=1000". Function-length rules
  (S138) and the CSS/HTML/SQL analyzers have no active size rule, and no exported
  issue for them exists.

File selection:

* every ``infra/tooling/sonar-project*.properties`` profile is read and the union
  of their main files is checked: ``sonar.sources`` minus ``sonar.exclusions``,
  restricted by ``sonar.inclusions`` and by each language's file suffixes, and
  for JS/TS minus SonarJS's default ``sonar.javascript.exclusions`` (``dist``,
  ``vendor``, ``node_modules`` ...). The tree is walked like the scanner walks
  it (SCM is disabled in every profile), so untracked files count too;
* test files are skipped: S104 never fires on them (the py profile analyzes
  ``tests/**`` -- 17k-line modules included -- and no S104 was ever raised there).

Counting (matches the numbers in Sonar's issue messages):

* Python counts physical lines the way the analyzer's end-of-file token does:
  the number of line terminators plus one. A 1000-line file that ends with a
  newline is reported as 1,001 lines -- a Python module must stay at 999 lines.
* JavaScript/TypeScript count lines of code: a line counts when any non-comment
  token touches it, and a token spanning several lines (a template literal, a
  multi-line string) counts every line it spans. Blank and comment-only lines do
  not count. The tokenizer here is a small lexer, not the TypeScript parser:
  regex literals are recognised heuristically and JSX text is lexed as code. Its
  errors lean to over-counting (a stray quote or backtick in JSX text extends a
  token), so a clean precheck still predicts a clean scan.

Existing debt: Sonar's quality gate fails on *new* issues, and an S104 issue is
new only when a file first crosses the limit. ``sonar-size-baseline.json`` lists
the files already over it; they are reported, not failed. A file not in the
baseline that is over the limit fails, and so does a baseline entry that is no
longer over it (remove the entry -- the baseline only shrinks).

Exit codes: 0 clean, 1 findings, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

DEFAULT_MAXIMUM = 1000
PROPERTIES_DIR = "infra/tooling"
PROPERTIES_GLOB = "sonar-project*.properties"
BASELINE_PATH = "infra/tooling/quality/sonar-size-baseline.json"

# Analyzer defaults used when a profile does not set the suffix property.
DEFAULT_SUFFIXES = {
    "py": (".py",),
    "js": (".js", ".jsx", ".cjs", ".mjs", ".vue"),
    "ts": (".ts", ".tsx", ".cts", ".mts"),
}
SUFFIX_PROPERTY = {
    "py": "sonar.python.file.suffixes",
    "js": "sonar.javascript.file.suffixes",
    "ts": "sonar.typescript.file.suffixes",
}
RULE_BY_LANGUAGE = {"py": "python:S104", "js": "javascript:S104", "ts": "typescript:S104"}
# SonarJS skips these for JS/TS unless ``sonar.javascript.exclusions`` overrides them.
DEFAULT_JS_EXCLUSIONS = (
    "**/node_modules/**,**/bower_components/**,**/dist/**,**/vendor/**,**/external/**,**/contrib/**"
)


# --------------------------------------------------------------------------- #
# Properties and path patterns
# --------------------------------------------------------------------------- #


def parse_properties(text: str) -> dict[str, str]:
    """Parse a Java ``.properties`` file (comments, ``=``/``:``, line continuation)."""
    values: dict[str, str] = {}
    logical = ""
    for raw in text.splitlines():
        line = raw.strip() if not logical else raw.lstrip()
        if not logical and (not line or line.startswith(("#", "!"))):
            continue
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2 == 1:
            logical += line[:-1]
            continue
        logical += line
        match = re.match(r"([^=:\s]+)\s*[=:\s]\s*(.*)", logical)
        if match:
            values[match.group(1)] = match.group(2).strip()
        logical = ""
    return values


def split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a Sonar wildcard pattern (``**``, ``*``, ``?``) against relative paths."""
    pattern = pattern.strip().lstrip("/")
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class SonarProfile:
    """The main-file selection of one Sonar properties file."""

    name: str
    sources: tuple[str, ...]
    inclusions: tuple[re.Pattern[str], ...]
    exclusions: tuple[re.Pattern[str], ...]
    tests: tuple[str, ...]
    suffixes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Directories whose whole subtree an exclusion removes; pruned during the walk.
    pruned_dirs: tuple[re.Pattern[str], ...] = ()
    js_exclusions: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def from_properties(cls, name: str, values: dict[str, str]) -> SonarProfile:
        exclusions = split_list(values.get("sonar.exclusions", ""))
        suffixes = {
            language: (
                split_list(values[SUFFIX_PROPERTY[language]])
                if values.get(SUFFIX_PROPERTY[language])
                else DEFAULT_SUFFIXES[language]
            )
            for language in DEFAULT_SUFFIXES
        }
        return cls(
            name=name,
            sources=tuple(s.strip("/") for s in split_list(values.get("sonar.sources", ""))),
            inclusions=tuple(
                compile_pattern(p) for p in split_list(values.get("sonar.inclusions", ""))
            ),
            exclusions=tuple(compile_pattern(p) for p in exclusions),
            tests=tuple(t.strip("/") for t in split_list(values.get("sonar.tests", ""))),
            suffixes=suffixes,
            pruned_dirs=tuple(compile_pattern(p[:-3]) for p in exclusions if p.endswith("/**")),
            js_exclusions=tuple(
                compile_pattern(p)
                for p in split_list(
                    values.get("sonar.javascript.exclusions", DEFAULT_JS_EXCLUSIONS)
                )
            ),
        )

    def language_of(self, relative_path: str) -> str | None:
        lowered = relative_path.lower()
        for language, suffixes in self.suffixes.items():
            if any(lowered.endswith(suffix.lower()) for suffix in suffixes):
                return language
        return None

    def selects(self, relative_path: str, language: str) -> bool:
        if not _under_any(relative_path, self.sources) or _under_any(relative_path, self.tests):
            return False
        if self.inclusions and not any(p.match(relative_path) for p in self.inclusions):
            return False
        if language in {"js", "ts"} and any(p.match(relative_path) for p in self.js_exclusions):
            return False
        return not any(p.match(relative_path) for p in self.exclusions)

    def prunes(self, relative_dir: str) -> bool:
        return any(p.match(relative_dir) for p in self.pruned_dirs)


def _under_any(relative_path: str, roots: Iterable[str]) -> bool:
    return any(
        root in {"", "."} or relative_path == root or relative_path.startswith(root + "/")
        for root in roots
    )


def load_profiles(root: Path, names: Iterable[str] = ()) -> list[SonarProfile]:
    wanted = {name.strip() for name in names if name.strip()}
    profiles: list[SonarProfile] = []
    for path in sorted((root / PROPERTIES_DIR).glob(PROPERTIES_GLOB)):
        stem = path.name.removeprefix("sonar-project").removesuffix(".properties")
        name = stem.lstrip(".") or "stable"
        if wanted and name not in wanted:
            continue
        values = parse_properties(path.read_text(encoding="utf-8"))
        profiles.append(SonarProfile.from_properties(name, values))
    return profiles


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def count_python_lines(text: str) -> int:
    """Lines as python:S104 reports them: line terminators + 1 (the EOF token's line)."""
    return len(_LINE_BREAK.findall(text)) + 1


_REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    }
)
_IDENTIFIER_START = re.compile(r"[A-Za-z_$\u0080-￿]")
_IDENTIFIER_RUN = re.compile(r"[A-Za-z0-9_$\u0080-￿]+")


class _JsLexer:
    """Collect the lines touched by non-comment tokens of a JS/TS source."""

    def __init__(self, text: str) -> None:
        self.text = _LINE_BREAK.sub("\n", text)
        self.index = 0
        self.line = 1
        self.code_lines: set[int] = set()
        # "{" for a block/object brace, "`" for a template substitution ``${``.
        self.braces: list[str] = []
        self.regex_allowed = True

    def run(self) -> set[int]:
        text = self.text
        if text.startswith("﻿"):
            self.index = 1
        if text.startswith("#!", self.index):
            self._skip_to_line_end()
        while self.index < len(text):
            char = text[self.index]
            if char == "\n":
                self.line += 1
                self.index += 1
            elif char.isspace():
                self.index += 1
            elif text.startswith("//", self.index):
                self._skip_to_line_end()
            elif text.startswith("/*", self.index):
                self._skip_block_comment()
            else:
                self._token(char)
        return self.code_lines

    # -- tokens ------------------------------------------------------------ #

    def _token(self, char: str) -> None:
        start_line = self.line
        text = self.text
        if char in "\"'":
            self._scan_string(char)
            self.regex_allowed = False
        elif char == "`":
            self.index += 1
            self._scan_template_chunk()
        elif char == "{":
            self.braces.append("{")
            self.index += 1
            self.regex_allowed = True
        elif char == "}":
            self.index += 1
            if self.braces and self.braces.pop() == "`":
                self._scan_template_chunk()
            else:
                self.regex_allowed = True
        elif char == "/" and self.regex_allowed:
            self._scan_regex()
            self.regex_allowed = False
        elif _IDENTIFIER_START.match(char) or char.isdigit():
            match = _IDENTIFIER_RUN.match(text, self.index)
            word = match.group(0) if match else char
            self.index += len(word)
            self.regex_allowed = word in _REGEX_PRECEDING_KEYWORDS
        else:
            self.index += 1
            self.regex_allowed = char not in ")]"
        self._mark(start_line, self.line)

    def _mark(self, first: int, last: int) -> None:
        self.code_lines.update(range(first, last + 1))

    def _skip_to_line_end(self) -> None:
        end = self.text.find("\n", self.index)
        self.index = len(self.text) if end < 0 else end

    def _skip_block_comment(self) -> None:
        end = self.text.find("*/", self.index + 2)
        stop = len(self.text) if end < 0 else end + 2
        self.line += self.text.count("\n", self.index, stop)
        self.index = stop

    def _scan_string(self, quote: str) -> None:
        text = self.text
        self.index += 1
        while self.index < len(text):
            char = text[self.index]
            if char == "\\":
                if text.startswith("\n", self.index + 1):
                    self.line += 1
                self.index += 2
            elif char == quote:
                self.index += 1
                return
            elif char == "\n":
                return  # unterminated (or JSX text): the token ends with the line
            else:
                self.index += 1

    def _scan_template_chunk(self) -> None:
        """Scan from just after a backtick or a substitution's ``}`` to ``${`` or the closing backtick."""
        text = self.text
        while self.index < len(text):
            char = text[self.index]
            if char == "\\":
                if text.startswith("\n", self.index + 1):
                    self.line += 1
                self.index += 2
            elif char == "`":
                self.index += 1
                self.regex_allowed = False
                return
            elif text.startswith("${", self.index):
                self.index += 2
                self.braces.append("`")
                self.regex_allowed = True
                return
            else:
                if char == "\n":
                    self.line += 1
                self.index += 1

    def _scan_regex(self) -> None:
        text = self.text
        self.index += 1
        in_class = False
        while self.index < len(text):
            char = text[self.index]
            if char == "\n":
                return
            if char == "\\":
                self.index += 2
                continue
            self.index += 1
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                flags = _IDENTIFIER_RUN.match(text, self.index)
                if flags:
                    self.index += len(flags.group(0))
                return


def count_js_lines_of_code(text: str) -> int:
    """Lines as javascript:S104 / typescript:S104 report them (lines of code)."""
    return len(_JsLexer(text).run())


def count_lines(language: str, text: str) -> int:
    if language == "py":
        return count_python_lines(text)
    return count_js_lines_of_code(text)


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SizeFinding:
    path: str
    rule: str
    lines: int


def iter_selected_files(root: Path, profiles: list[SonarProfile]) -> Iterator[tuple[str, str]]:
    """Yield ``(relative_path, language)`` for every main file any profile analyzes."""
    seen: set[str] = set()
    for profile in profiles:
        for source in profile.sources:
            base = root / source
            if base.is_file():
                candidates: Iterable[str] = (source,)
            elif base.is_dir():
                candidates = _walk(root, base, profile)
            else:
                continue
            for relative_path in candidates:
                if relative_path in seen:
                    continue
                language = profile.language_of(relative_path)
                if language and profile.selects(relative_path, language):
                    seen.add(relative_path)
                    yield relative_path, language


def _walk(root: Path, base: Path, profile: SonarProfile) -> Iterator[str]:
    root_text = str(root)
    for directory, dirnames, filenames in os.walk(base):
        relative_dir = os.path.relpath(directory, root_text).replace(os.sep, "/")
        dirnames[:] = sorted(
            name for name in dirnames if not profile.prunes(f"{relative_dir}/{name}")
        )
        for name in sorted(filenames):
            yield f"{relative_dir}/{name}"


def scan(
    root: Path, profiles: list[SonarProfile], maximum: int
) -> tuple[list[SizeFinding], set[str]]:
    """Return the files over ``maximum`` and every file the profiles select."""
    findings: list[SizeFinding] = []
    selected: set[str] = set()
    for relative_path, language in iter_selected_files(root, profiles):
        selected.add(relative_path)
        try:
            text = (root / relative_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Lines of code never exceed physical lines: skip the lexer for short files.
        if text.count("\n") + 1 <= maximum:
            continue
        lines = count_lines(language, text)
        if lines > maximum:
            findings.append(SizeFinding(relative_path, RULE_BY_LANGUAGE[language], lines))
    findings.sort(key=lambda finding: finding.path)
    return findings, selected


# --------------------------------------------------------------------------- #
# Baseline and report
# --------------------------------------------------------------------------- #


def load_baseline(path: Path) -> tuple[int, frozenset[str]]:
    if not path.is_file():
        return DEFAULT_MAXIMUM, frozenset()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return int(payload.get("maximum", DEFAULT_MAXIMUM)), frozenset(payload.get("files", ()))


def write_baseline(path: Path, maximum: int, findings: list[SizeFinding]) -> None:
    payload = {
        "description": (
            "Files already over the Sonar S104 limit. Sonar's gate fails only on new "
            "issues, so these are reported, not failed. Remove an entry when its file "
            "drops under the limit; never add one to silence a new finding."
        ),
        "maximum": maximum,
        "files": sorted(finding.path for finding in findings),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _describe(finding: SizeFinding, maximum: int) -> str:
    return f"{finding.path}: {finding.rule} {finding.lines} lines > {maximum}"


def run_sonar_size_precheck(
    root: Path,
    *,
    profiles: Iterable[str] = (),
    baseline_path: Path | None = None,
    use_baseline: bool = True,
    update_baseline: bool = False,
    out: TextIO | None = None,
) -> int:
    stream = out if out is not None else sys.stdout
    started = time.monotonic()
    selected = load_profiles(root, profiles)
    if not selected:
        print(
            f"[sonar-size] No Sonar profile found under {root / PROPERTIES_DIR}.", file=sys.stderr
        )
        return 2
    baseline_file = baseline_path or (root / BASELINE_PATH)
    maximum, baseline = load_baseline(baseline_file)
    findings, scope = scan(root, selected, maximum)

    if update_baseline:
        write_baseline(baseline_file, maximum, findings)
        print(f"[sonar-size] Wrote {len(findings)} file(s) to {baseline_file}.", file=stream)
        return 0

    if not use_baseline:
        baseline = frozenset()
    over = {finding.path for finding in findings}
    known = over & baseline
    new = [finding for finding in findings if finding.path not in known]
    # With every profile selected, an entry for a deleted or renamed file is stale
    # too; a --profile run judges only the entries inside its own selection.
    whole_tree = not [name for name in profiles if name.strip()]
    stale = sorted(path for path in baseline - over if whole_tree or path in scope)

    for finding in findings:
        if finding.path in known:
            print(f"  known  {_describe(finding, maximum)}", file=stream)
    for finding in new:
        print(f"  NEW    {_describe(finding, maximum)}", file=stream)
    for path in stale:
        print(
            f"  STALE  {path}: in {BASELINE_PATH} but not over {maximum} lines; remove the entry",
            file=stream,
        )
    elapsed = time.monotonic() - started
    summary = (
        f"[sonar-size] profiles={','.join(p.name for p in selected)} files={len(scope)} "
        f"over={len(findings)} known={len(known)} new={len(new)} stale={len(stale)} "
        f"({elapsed:.1f}s)"
    )
    if new or stale:
        print(f"{summary}: FAIL", file=stream)
        if new:
            print(
                "[sonar-size] Split each NEW file below the limit: Sonar raises S104 on it as a "
                "new issue and the quality gate fails. Python counts physical lines + 1 "
                f"(a module must stay at {maximum - 1} lines); JS/TS counts lines of code.",
                file=stream,
            )
        return 1
    print(f"{summary}: OK", file=stream)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[4]),
        help="repository root (default: this checkout)",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="limit to a Sonar profile (stable, js, py, full); repeatable; default: all",
    )
    parser.add_argument("--baseline", default="", help=f"baseline file (default: {BASELINE_PATH})")
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="fail on every file over the limit, including known ones",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline from the current tree (review the diff)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    return run_sonar_size_precheck(
        root,
        profiles=args.profile,
        baseline_path=Path(args.baseline).resolve() if args.baseline else None,
        use_baseline=not args.no_baseline,
        update_baseline=args.update_baseline,
    )


if __name__ == "__main__":
    raise SystemExit(main())
