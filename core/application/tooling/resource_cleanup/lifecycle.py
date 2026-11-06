"""Automatic end-of-command reconciliation, keeping execution and cleanup outcomes distinct."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4

from core.application.tooling.resource_cleanup.service import ResourceCleanup


@dataclass(frozen=True)
class RunResult:
    command_exit_code: int
    cleanup_ok: bool

    @property
    def exit_code(self) -> int:
        return self.command_exit_code or (0 if self.cleanup_ok else 2)


def run_owned(service: ResourceCleanup, owner: str, command: list[str]) -> RunResult:
    """Release only this task's unretained resources when its foreground command exits."""
    if not command:
        raise ValueError("run requires a command")
    service.initialize(owner)
    native = service.native
    with service.journal.locked():
        ledger = service.journal.load(owner)
        if ledger.run_pid and native.process_alive(ledger.run_pid):
            raise ValueError("task already has an active execution")
        if ledger.run_pgid and native.process_group_alive(ledger.run_pgid):
            raise ValueError("task still has active child processes")
        ledger.run_pid = native.current_pid()
        ledger.run_pgid = None
        service.journal.save(ledger)
    code = 127
    discovery_error = None
    borrowed_for_run: set[str] = set()
    try:
        run_id = uuid4().hex[:12]
        scratch = service.create_directory(owner, "run-" + run_id)
        cache = service.create_directory(owner, "cache-" + run_id)
        environment = native.execution_environment()
        environment.update(
            {
                "FS_TASK_RESOURCE_OWNER": owner,
                "FS_TASK_RESOURCE_TOKEN": ledger.token,
                "FS_TASK_RESOURCE_REGISTRY": str(service.journal.root),
                "FS_TASK_IMAGE_PREFIX": "fs-owned/" + ledger.token,
                "FS_TASK_SCRATCH": scratch.identity,
                "XDG_CACHE_HOME": cache.identity,
                "TMPDIR": scratch.identity,
                "PIP_CACHE_DIR": cache.identity + "/pip",
                "npm_config_cache": cache.identity + "/npm",
            }
        )
        environment = native.prepare_environment(
            ledger, command, environment, scratch.identity, cache.identity
        )
        borrowed_image = environment.get("FS_BORROWED_AUDIT_IMAGE")
        if borrowed_image:
            resource = service.track(owner, "image_tag", borrowed_image, borrowed=True)
            borrowed_for_run.add(resource.resource_id)
        with service.journal.locked():
            ledger = service.journal.load(owner)
        builders = [
            r
            for r in ledger.resources
            if r.kind == "builder" and r.state in {"active", "retained"} and r.disposable
        ]
        if (
            environment.get("FS_OWNED_QUALITY") == ledger.token
            and not borrowed_image
            and not builders
        ):
            builders = [service.create_builder(owner)]
        if builders and not borrowed_image:
            if not native.validate(ledger, builders[0]):
                raise ValueError("private builder is unavailable")
            environment["BUILDX_BUILDER"] = builders[0].identity

        def started(pgid: int) -> None:
            with service.journal.locked():
                current = service.journal.load(owner)
                current.run_pgid = pgid
                service.journal.save(current)

        try:
            code = native.execute_owned(command, environment, started)
        except FileNotFoundError:
            code = 127
        except PermissionError:
            code = 126
    finally:
        try:
            service.reconcile_created(owner)
        except (OSError, ValueError) as exc:
            discovery_error = str(exc)
        with service.journal.locked():
            current = service.journal.load(owner)
            children_alive = bool(current.run_pgid and native.process_group_alive(current.run_pgid))
            if not children_alive:
                current.run_pid = None
                current.run_pgid = None
                for resource in current.resources:
                    if (
                        resource.resource_id in borrowed_for_run
                        and resource.state == "active"
                        and not resource.consumers
                    ):
                        resource.state = "released"
                    if (
                        discovery_error is None
                        and resource.disposable
                        and resource.state == "active"
                        and not resource.consumers
                    ):
                        resource.state = "released"
            service.journal.save(current)
        receipt = service.cleanup(owner, apply=True)
        if discovery_error is not None:
            receipt.blocked["discovery"] = discovery_error
        with service.journal.locked():
            service.journal.receipt(owner, {**asdict(receipt), "command_exit_code": code})
    return RunResult(code, not receipt.blocked)
