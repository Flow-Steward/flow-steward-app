"""An extension release update keeps every bound hook meaningful (spec §12).

Before a release's workflows are projected, every configured binding of the
install is checked against the release's own hook declarations:

* the hook still exists under the same stable id;
* its semantic contract (schemas, mappings, mutability, output-only fields,
  defaults, stage) is unchanged — or the client staged a binding revision for
  exactly this release whose digest matches the new contract.

Anything else blocks the activation with the consumer and reason; nothing is
disabled or rewritten silently. The staged revisions become active in the same
transaction as the release, and until then the previous bindings keep working.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.workflows.workflow_hook_binding_repository import (
    WorkflowHookBindingRepository,
)
from core.application.workflows.workflow_hook_contract import (
    HOOK_CONFIG_KEY,
    HookContractError,
    hook_semantic_digest,
    parse_hook_declaration,
)


class HookReleaseBlockedError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__(
            "The release would break connected hooks: "
            + "; ".join(issue["message"] for issue in issues[:5])
        )
        self.issues = issues


def release_hook_digests(workflows: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """``(local workflow id, hook id)`` → semantic digest of every hook a release declares."""
    digests: dict[tuple[str, str], str] = {}
    for local_id, workflow in workflows.items():
        for step in workflow.get("steps") or []:
            override = step.get("override") if isinstance(step, Mapping) else None
            config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
            raw = config.get(HOOK_CONFIG_KEY) if isinstance(config, Mapping) else None
            if not isinstance(raw, Mapping):
                continue
            try:
                hook = parse_hook_declaration(raw)
            except HookContractError:
                continue  # the release's own validation refuses it
            digests[(str(local_id), hook.hook_id)] = hook_semantic_digest(hook)
    return digests


def installed_hook_digests(
    digests: Mapping[tuple[str, str], str], *, install_id: str
) -> dict[tuple[str, str], str]:
    """*digests* under the release's own workflow ids and their installed ids.

    A release names its workflows by their own id; a binding made on the
    installed workflow may name it ``extension.<install>.<id>``, as every
    binding on a live install did before 0.7.25. Both mean the same workflow,
    so an update must not read the hook as removed.
    """
    installed = dict(digests)
    install = str(install_id or "").strip()
    if install:
        for (local_id, hook_id), digest in digests.items():
            installed.setdefault((f"extension.{install}.{local_id}", hook_id), digest)
    return installed


def release_hook_blockers(
    bindings: Iterable[Mapping[str, Any]],
    *,
    release_id: str,
    digests: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """Why activating *release_id* would break a configured binding."""
    issues: list[dict[str, Any]] = []
    for row in bindings:
        if not row.get("active_target"):
            continue  # an empty hook has nothing to break
        key = (str(row["local_workflow_id"]), str(row["hook_id"]))
        new_digest = digests.get(key)
        staged = row.get("staged_release_id") == release_id
        label = (
            f"hook '{row['hook_id']}' of '{row['local_workflow_id']}' "
            f"in project {row['project_id']}"
        )
        if new_digest is None:
            if not (staged and row.get("staged_target") is None):
                issues.append(
                    {
                        "code": "workflow.hook.release_removes_hook",
                        "message": f"{label}: the release no longer declares this hook",
                        "binding": dict(row),
                    }
                )
            continue
        if new_digest == row.get("active_digest"):
            continue
        if staged and row.get("staged_digest") == new_digest:
            continue
        issues.append(
            {
                "code": "workflow.hook.release_contract_changed",
                "message": (
                    f"{label}: the release changes the hook contract; prepare a binding "
                    "for the new release first"
                ),
                "binding": dict(row),
            }
        )
    return issues


def check_and_activate_release_hooks(
    conn: Any,
    *,
    account_id: Any,
    install_id: str,
    release_id: str,
    workflows: Mapping[str, Mapping[str, Any]],
) -> int:
    """Refuse a breaking release, else activate its staged bindings (same transaction)."""
    repository = WorkflowHookBindingRepository(conn)
    bindings = repository.install_bindings(account_id=account_id, install_id=install_id)
    if not bindings:
        return 0
    issues = release_hook_blockers(
        bindings,
        release_id=release_id,
        digests=installed_hook_digests(release_hook_digests(workflows), install_id=install_id),
    )
    if issues:
        raise HookReleaseBlockedError(issues)
    return repository.activate_staged(
        account_id=account_id, install_id=install_id, release_id=release_id
    )


__all__ = [
    "HookReleaseBlockedError",
    "check_and_activate_release_hooks",
    "installed_hook_digests",
    "release_hook_blockers",
    "release_hook_digests",
]
