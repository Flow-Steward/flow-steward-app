"""Per-module map of the repository files each Tier 3 test module used (ADR 0068).

Built from the ``impact_recorder`` output of a run and carried to the next run
next to the check's evidence. A full run replaces the map; a selected run
replaces the entries of the modules it ran and keeps the rest.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from core.application.tooling.quality.impact_recorder import (
    MAP_PREFIX,
    RESULT_FILE,
    SHARED_CONTEXT,
)

MAP_SCHEMA = 1


@dataclass
class ImpactMap:
    """Test module -> repository files it executed, opened or launched."""

    commit: str = ""
    modules: dict[str, frozenset[str]] = field(default_factory=dict)
    shared: frozenset[str] = frozenset()

    def tests_using(self, path: str) -> set[str]:
        """Modules that used ``path`` itself or listed the directory holding it."""
        parent = str(PurePosixPath(path).parent)
        return {
            module for module, files in self.modules.items() if path in files or parent in files
        }

    def tests_using_under(self, prefix: str) -> set[str]:
        """Modules that used any file below ``prefix`` (``assets/`` for the built shell)."""
        return {
            module
            for module, files in self.modules.items()
            if any(item.startswith(prefix) for item in files)
        }

    def is_shared(self, path: str) -> bool:
        return path in self.shared or str(PurePosixPath(path).parent) in self.shared

    def used(self, path: str) -> bool:
        return self.is_shared(path) or any(path in files for files in self.modules.values())

    def updated(self, run: RecordedRun, *, commit: str, full: bool) -> ImpactMap:
        modules = {} if full else dict(self.modules)
        modules.update(run.modules)
        shared = run.shared if full else self.shared | run.shared
        return ImpactMap(commit=commit, modules=modules, shared=shared)

    def dump(self, path: Path) -> None:
        files = sorted(
            {item for entries in self.modules.values() for item in entries} | self.shared
        )
        index = {item: position for position, item in enumerate(files)}
        payload = {
            "schema": MAP_SCHEMA,
            "commit": self.commit,
            "files": files,
            "shared": sorted(index[item] for item in self.shared),
            "modules": {
                module: sorted(index[item] for item in entries)
                for module, entries in sorted(self.modules.items())
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path) -> ImpactMap | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") != MAP_SCHEMA:
                return None
            files = payload["files"]
            return cls(
                commit=str(payload["commit"]),
                modules={
                    str(module): frozenset(files[item] for item in entries)
                    for module, entries in payload["modules"].items()
                },
                shared=frozenset(files[item] for item in payload["shared"]),
            )
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None


@dataclass(frozen=True)
class RecordedRun:
    """What one pytest run reported through ``impact_recorder``."""

    complete: bool
    exitstatus: int | None
    failed: tuple[str, ...]
    modules: dict[str, frozenset[str]]
    shared: frozenset[str]

    @property
    def usable_failures(self) -> bool:
        """Test failures only (exit 1) with every failing module named."""
        return self.complete and self.exitstatus == 1 and bool(self.failed)


def read_recorded_run(directory: Path) -> RecordedRun:
    """Merge the per-worker maps and the controller result of one run."""
    modules: dict[str, set[str]] = {}
    shared: set[str] = set()
    complete = True
    shards = sorted(directory.glob(f"{MAP_PREFIX}*.json")) if directory.is_dir() else []
    for shard in shards:
        payload = _read_json(shard)
        if not isinstance(payload, dict) or payload.get("complete") is not True:
            complete = False
            continue
        contexts = payload.get("contexts", {})
        for module in payload.get("modules", ()):
            modules.setdefault(str(module), set())
        for context, files in contexts.items():
            target = shared if context == SHARED_CONTEXT else modules.setdefault(context, set())
            target.update(str(item) for item in files)
    result = _read_json(directory / RESULT_FILE)
    if not isinstance(result, dict):
        result = {}
    # A vitest result carries no map: vitest follows its own module graph.
    maps_expected = result.get("maps", True) is not False
    if result.get("recorded") is not True or (maps_expected and not shards):
        complete = False
    exitstatus = result.get("exitstatus")
    return RecordedRun(
        complete=complete,
        exitstatus=exitstatus if isinstance(exitstatus, int) else None,
        failed=tuple(sorted(str(item) for item in result.get("failed", ()))),
        modules={module: frozenset(files) for module, files in modules.items()},
        shared=frozenset(shared),
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
