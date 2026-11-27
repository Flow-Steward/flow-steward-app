"""Dependencies between platform modules (ADR 0082, 2026-11-27 amendment).

A module descriptor may declare ``"requires": ["<module id>", ...]``: the modules it
needs loaded before it. An item may add a minimum version, ``"<module id>>=1.2"``.
This module holds the pure rules shared by the loader and the module store: reading
the declaration, ordering a module set by its dependencies (topologically, module
id breaking ties) and naming why a module cannot load (a required module that is
missing, failed, too old, or part of a dependency cycle).

It imports nothing from ``core.application`` and nothing from the loader.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from core.infrastructure.extension_runtime.package_identity import CANONICAL_EXTENSION_ID_RE

_REQUIREMENT_PATTERN = re.compile(r"^(?P<id>[^\s>=]+)(?:\s*>=\s*(?P<minimum>\d+(?:\.\d+)*))?$")
_LEADING_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*")


@dataclass(frozen=True, slots=True)
class ModuleRequirement:
    """One declared dependency: a module id and an optional minimum version."""

    module_id: str
    minimum_version: str = ""

    def describe(self) -> str:
        if self.minimum_version:
            return f"{self.module_id}>={self.minimum_version}"
        return self.module_id

    def satisfied_by(self, version: str) -> bool:
        """Whether ``version`` meets the minimum; a non-numeric version never does."""
        if not self.minimum_version:
            return True
        found = _LEADING_VERSION_PATTERN.match(version.strip())
        if found is None:
            return False
        have = [int(part) for part in found.group(0).split(".")]
        need = [int(part) for part in self.minimum_version.split(".")]
        width = max(len(have), len(need))
        have += [0] * (width - len(have))
        need += [0] * (width - len(need))
        return have >= need


def parse_module_requirements(value: object, *, module_id: str) -> tuple[ModuleRequirement, ...]:
    """Read a ``"requires"`` list; raise ``ValueError`` naming what is wrong."""
    if not isinstance(value, list):
        raise ValueError("must be a list of module ids")
    requirements: list[ModuleRequirement] = []
    seen: set[str] = set()
    for item in value:
        match = _REQUIREMENT_PATTERN.fullmatch(item.strip()) if isinstance(item, str) else None
        if match is None or not CANONICAL_EXTENSION_ID_RE.fullmatch(match.group("id")):
            raise ValueError(
                f"names {item!r}; use a module id, optionally with a minimum version "
                "such as 'acme.base>=1.2'"
            )
        required_id = match.group("id")
        if required_id == module_id:
            raise ValueError(f"names the module itself ('{module_id}')")
        if required_id in seen:
            raise ValueError(f"names '{required_id}' twice")
        seen.add(required_id)
        requirements.append(ModuleRequirement(required_id, match.group("minimum") or ""))
    return tuple(requirements)


@dataclass(frozen=True, slots=True)
class DependencyResolution:
    """A module set ordered by its dependencies, and the modules that cannot load.

    ``order`` lists the loadable module ids, every module after the modules it
    requires, module id breaking ties. ``problems`` maps each module that cannot
    load to its reason, which starts with ``requires <id>``.
    """

    order: tuple[str, ...]
    problems: Mapping[str, str]


def resolve_module_dependencies(
    modules: Mapping[str, tuple[str, Iterable[ModuleRequirement]]],
    *,
    failed: Iterable[str] = (),
) -> DependencyResolution:
    """Order ``modules`` (id -> (version, requirements)) by their dependencies.

    A module whose required module is not in ``modules``, is in ``failed``, is older
    than the declared minimum, or cannot load itself, cannot load either; so can no
    module in a dependency cycle.
    """
    failed_ids = set(failed)
    requirements = {
        module_id: tuple(requires) for module_id, (_version, requires) in modules.items()
    }
    problems: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for module_id in sorted(requirements):
            if module_id in problems:
                continue
            reason = _unmet_requirement(module_id, requirements, modules, problems, failed_ids)
            if reason:
                problems[module_id] = reason
                changed = True

    remaining = sorted(module_id for module_id in requirements if module_id not in problems)
    dependents: dict[str, list[str]] = {module_id: [] for module_id in remaining}
    waiting: dict[str, int] = {}
    for module_id in remaining:
        needed = {item.module_id for item in requirements[module_id]}
        waiting[module_id] = len(needed)
        for required_id in needed:
            dependents[required_id].append(module_id)
    ready = [module_id for module_id in remaining if waiting[module_id] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        module_id = heapq.heappop(ready)
        order.append(module_id)
        for dependent in dependents[module_id]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)

    ordered = set(order)
    blocked = [module_id for module_id in remaining if module_id not in ordered]
    for module_id in blocked:
        required = next(
            item.module_id for item in requirements[module_id] if item.module_id in blocked
        )
        problems[module_id] = f"requires {required} (dependency cycle among {', '.join(blocked)})"
    return DependencyResolution(order=tuple(order), problems=dict(sorted(problems.items())))


def _unmet_requirement(
    module_id: str,
    requirements: Mapping[str, tuple[ModuleRequirement, ...]],
    modules: Mapping[str, tuple[str, Iterable[ModuleRequirement]]],
    problems: Mapping[str, str],
    failed_ids: set[str],
) -> str:
    for item in requirements[module_id]:
        required_id = item.module_id
        if required_id in failed_ids or required_id in problems:
            return f"requires {required_id} (which cannot load)"
        if required_id not in modules:
            return f"requires {required_id} (not in the module set)"
        version = modules[required_id][0]
        if not item.satisfied_by(version):
            return f"requires {item.describe()} ({required_id} {version} is in the set)"
    return ""


__all__ = [
    "DependencyResolution",
    "ModuleRequirement",
    "parse_module_requirements",
    "resolve_module_dependencies",
]
