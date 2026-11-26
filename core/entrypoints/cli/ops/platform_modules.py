"""Platform-module commands that run inside a role image (ADR 0082 §5).

On Compact the runtime directory that holds ``modules.json`` is inside the
``compact-runtime`` container's data volume, which the operator CLI cannot enter. The
release workflow therefore runs this module in a disposable container of the installed
runtime image (``DockerLifecycleGateway.platform_module_command``)::

    python -m core.entrypoints.cli.ops.platform_modules installed
    python -m core.entrypoints.cli.ops.platform_modules staged
    python -m core.entrypoints.cli.ops.platform_modules verify --modules '<set>'
    python -m core.entrypoints.cli.ops.platform_modules activate --modules '<set>'
    python -m core.entrypoints.cli.ops.platform_modules adds-migrations \
        --previous '<set>' --target '<set>'

``<set>`` is a JSON list of ``{"id", "version", "digest"}``. Every command prints one
JSON document on stdout: ``{"ok": true, "command": ..., "result": {...}}``, or
``{"ok": false, "error": {"code", "message", "hint"}}`` with exit status 1. The
filesystem work is ``PlatformModuleStore``'s, the same code the Scalable CLI runs in
its own process; here the loader applies its image checks, because this process runs
in the image every Compact role runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.platform_modules.loader import platform_modules_root
from core.infrastructure.platform_modules.module_store import (
    ListOwnership,
    ModuleRef,
    ModuleStoreError,
    PlatformModuleStore,
    parse_module_set,
)
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout

#: Compact's runtime directory files the roles only read: the operator owns them, the
#: roles read them through the runtime group (as ``maintenance-state.json``).
_COMPACT_LIST_MODE = 0o640
_HINT = (
    "Keep maintenance enabled, let the deployment manager stage the pinned platform "
    "modules again, then repeat the operation."
)


def _ownership() -> ListOwnership | None:
    """The list ownership the roles of this image expect; ``None`` keeps the old rule."""
    if str(_runtime_env.get("FS_DEPLOYMENT_PROFILE") or "").strip() != "compact":
        return None
    layout = CompactVolumeLayout()
    return ListOwnership(uid=layout.operator_uid, gid=layout.runtime_gid, mode=_COMPACT_LIST_MODE)


def _store() -> PlatformModuleStore:
    return PlatformModuleStore(platform_modules_root(), image_checks=True, ownership=_ownership())


def _module_set(text: str) -> tuple[ModuleRef, ...]:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ModuleStoreError(
            "release_module_set_invalid", f"The platform module set is not JSON: {exc}"
        ) from exc
    return parse_module_set(raw)


def _listing(refs: Sequence[ModuleRef]) -> dict[str, Any]:
    return {"modules": [ref.to_json() for ref in refs]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="platform_modules", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("installed", add_help=False)
    commands.add_parser("staged", add_help=False)
    for name in ("verify", "activate"):
        command = commands.add_parser(name, add_help=False)
        command.add_argument("--modules", required=True)
    adds = commands.add_parser("adds-migrations", add_help=False)
    adds.add_argument("--previous", required=True)
    adds.add_argument("--target", required=True)
    return parser


def run(arguments: argparse.Namespace, store: PlatformModuleStore) -> dict[str, Any]:
    """Run one parsed command against ``store`` and return its ``result`` object."""
    if arguments.command == "installed":
        return _listing(store.installed_refs())
    if arguments.command == "staged":
        return _listing(store.staged_refs())
    if arguments.command == "verify":
        modules = _module_set(arguments.modules)
        store.verify(modules)
        return _listing(modules)
    if arguments.command == "activate":
        modules = _module_set(arguments.modules)
        changed = store.activate(modules)
        return {**_listing(modules), "changed": changed}
    previous = _module_set(arguments.previous)
    target = _module_set(arguments.target)
    return {"adds_migrations": store.adds_module_migrations(previous, target)}


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None, *, store: PlatformModuleStore | None = None) -> int:
    try:
        arguments = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "release_module_command_invalid",
                    "message": "Unknown platform-module command or arguments.",
                    "hint": "",
                },
            }
        )
        return 2
    try:
        result = run(arguments, store if store is not None else _store())
    except ModuleStoreError as exc:
        _emit({"ok": False, "error": {"code": exc.code, "message": exc.cause, "hint": _HINT}})
        return 1
    except OSError as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "release_module_command_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "hint": _HINT,
                },
            }
        )
        return 1
    _emit({"ok": True, "command": arguments.command, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
