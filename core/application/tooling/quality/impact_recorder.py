"""Pytest plugin recording which repository files each test module uses (ADR 0068).

Loaded by the Tier 3 backend suites with ``-p``. It records, per test module,
the repository files whose functions ran (``sys.monitoring``, first call per
function per module, so it costs little next to coverage) and the repository
files it opened, listed or launched. Code that runs before a module's own tests
-- collection, session and package fixtures -- is recorded under ``@shared``:
a change there can reach every test, so the selection runs the whole suite.

Import-time module and class bodies are not recorded: imports happen once per
worker in whichever module comes first. The selection covers them by following
the import graph from a changed module level (``impact_selection``).
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from collections.abc import Generator
from inspect import CO_OPTIMIZED
from pathlib import Path
from types import CodeType
from typing import Any

import pytest

SHARED_CONTEXT = "@shared"
MAP_PREFIX = "map-"
RESULT_FILE = "result.json"
_TOOL_NAME = "flow-steward-test-impact"
_BROAD_SCOPES = frozenset({"session", "package"})
_OPEN_IGNORED_SUFFIXES = (".py", ".pyc", ".pyi", ".so")
_OPTION = "--fs-test-impact-output"
# sys.monitoring exists from Python 3.12 (the gate's runtime); the tools check 3.11.
_MONITORING: Any = getattr(sys, "monitoring", None)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(_OPTION, default="", help="Directory for the per-module file map.")


def pytest_configure(config: pytest.Config) -> None:
    output = str(config.getoption(_OPTION) or "")
    if output:
        config.pluginmanager.register(_ImpactRecorder(config, Path(output)), "fs-test-impact")


class _ImpactRecorder:
    def __init__(self, config: pytest.Config, output: Path) -> None:
        self.output = output
        self.root = str(config.rootpath.resolve())
        self.prefixes = tuple({self.root + os.sep, os.path.realpath(self.root) + os.sep})
        worker_input = getattr(config, "workerinput", None)
        self.is_controller = worker_input is None
        self.worker = str(worker_input.get("workerid", "worker")) if worker_input else "main"
        self.context = SHARED_CONTEXT
        self.files: dict[str, set[str]] = defaultdict(set)
        self.failed: set[str] = set()
        self.ran: set[str] = set()
        self.tool_id: int | None = None
        # An xdist controller runs no tests; it only collects the reports.
        if not (self.is_controller and config.getoption("numprocesses", default=None)):
            self.tool_id = self._start_monitoring()
            sys.addaudithook(self._audit)

    # -- recording -------------------------------------------------------------

    def _start_monitoring(self) -> int | None:
        monitoring = _MONITORING
        if monitoring is None:
            return None
        for tool_id in (monitoring.PROFILER_ID, 3, 4):
            try:
                monitoring.use_tool_id(tool_id, _TOOL_NAME)
            except ValueError:
                continue
            monitoring.register_callback(tool_id, monitoring.events.PY_START, self._on_start)
            monitoring.set_events(tool_id, monitoring.events.PY_START)
            return tool_id
        return None

    def _relative(self, path: str) -> str:
        for prefix in self.prefixes:
            if path.startswith(prefix):
                return path[len(prefix) :]
        return ""

    def _on_start(self, code: CodeType, _offset: int) -> object:
        # Module and class bodies run at import, once per worker: not a test's use.
        if code.co_flags & CO_OPTIMIZED:
            relative = self._relative(code.co_filename)
            if relative:
                self.files[self.context].add(relative)
        return _MONITORING.DISABLE

    def _audit(self, event: str, args: tuple[Any, ...]) -> None:
        if self.tool_id is None or event not in {
            "open",
            "os.listdir",
            "os.scandir",
            "subprocess.Popen",
        }:
            return
        try:
            if event == "subprocess.Popen":
                self._record_command(args)
            elif args and isinstance(args[0], str | bytes | os.PathLike):
                self._record_path(os.fsdecode(args[0]))
        except Exception:
            return

    def _record_path(self, raw: str, cwd: str = "", *, launched: bool = False) -> None:
        path = os.path.abspath(os.path.join(cwd, raw) if cwd else raw)
        # Imports open source files too; a test's use of Python code is recorded
        # through sys.monitoring, unless the file is launched as a program.
        if "__pycache__" in path or (not launched and path.endswith(_OPEN_IGNORED_SUFFIXES)):
            return
        relative = self._relative(path)
        if relative:
            self.files[self.context].add(relative)

    def _record_command(self, args: tuple[Any, ...]) -> None:
        executable, argv, cwd = args[0], args[1], args[2]
        base = os.fsdecode(cwd) if cwd else os.getcwd()
        parts = [executable] if executable else []
        parts += [argv] if isinstance(argv, str | bytes) else list(argv or ())
        previous = ""
        for part in parts:
            if not isinstance(part, str | bytes | os.PathLike):
                continue
            candidate = os.fsdecode(part)
            if previous == "-m":
                # `python -m package.module` runs repository code in the child.
                self._record_module(candidate)
            elif os.path.exists(os.path.join(base, candidate)):
                self._record_path(candidate, base, launched=True)
            previous = candidate

    def _record_module(self, dotted: str) -> None:
        relative = dotted.replace(".", os.sep)
        for suffix in (".py", os.sep + "__main__.py", os.sep + "__init__.py"):
            path = os.path.join(self.root, relative + suffix)
            if os.path.isfile(path):
                self.files[self.context].add(self._relative(path) or relative + suffix)
                return

    def _switch(self, context: str) -> None:
        if context != self.context:
            self.context = context
            if self.tool_id is not None:
                # Functions already seen elsewhere are recorded again here.
                _MONITORING.restart_events()

    # -- pytest hooks ------------------------------------------------------------

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> None:
        # Switching only between modules keeps restart_events() off the per-test path.
        module = item.nodeid.split("::", 1)[0]
        self.ran.add(module)
        self._switch(module)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
    ) -> Generator[None, object, object]:
        del request
        previous = self.context
        if fixturedef.scope in _BROAD_SCOPES:
            self._switch(SHARED_CONTEXT)
        try:
            return (yield)
        finally:
            self._switch(previous)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            self.failed.add(report.nodeid.split("::", 1)[0])

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.failed.add(report.nodeid.split("::", 1)[0])

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        if self.tool_id is not None:
            _MONITORING.set_events(self.tool_id, 0)
            mapping = {context: sorted(files) for context, files in self.files.items()}
            payload = {"complete": True, "modules": sorted(self.ran), "contexts": mapping}
            _write_json(self.output / f"{MAP_PREFIX}{self.worker}.json", payload)
        if self.is_controller:
            result = {
                "exitstatus": int(exitstatus),
                "failed": sorted(self.failed),
                "recorded": self.tool_id is not None,
                "testscollected": session.testscollected,
            }
            _write_json(self.output / RESULT_FILE, result)


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
