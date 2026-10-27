"""`./install.sh --deployment-profile` on an installed system (item 5, step 4).

ADR 0029, decision B: a switch between Compact and Scalable keeps the shared trees --
artifacts and extensions -- where they are, and the target profile
mounts them there (step 2); nothing is copied. The switch itself is the lifecycle's
`config apply`, which journals every step, preserves the database, broker and volumes,
and rolls back on failure. It runs where it always runs, in the Scalable CLI container:
the installer's own image has no database client. What the installer adds is what
`config apply` does not do:

- it prepares the target profile's images, with the installer's own build;
- Scalable to Compact: it brings the host trees into the shared groups and modes
  (step 3) and records where they are for the Compact overlay;
- Compact to Scalable: it starts a Scalable CLI container;
- it records the target with the placement kept, and lends the lifecycle lock to the
  apply for its duration (./flow-steward stays refused meanwhile by install.sh's
  host record).

Interrupted anywhere, the next ./install.sh continues: every preparation is idempotent,
and an unfinished apply is resumed from its own journal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.installer.contracts import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerGateway, InstallerLog
from core.application.lifecycle.contracts import ConfigurationPatch


def switch_target(installed: InstallConfig, requested_profile: str) -> InstallConfig:
    """The installed configuration on the other profile, with its trees where they are."""
    values = installed.model_dump(mode="json")
    values["deployment_profile"] = requested_profile
    values["data_placement"] = installed.data_placement
    if requested_profile == "compact":
        values["worker_replicas"] = 1  # Compact scales worker processes inside it
    return InstallConfig.model_validate(values)


def switch_profile(
    gateway: InstallerGateway,
    installed: InstallConfig,
    requested_profile: str,
    *,
    log: InstallerLog,
) -> tuple[str, ...]:
    """Switch an installed system to the requested profile; what changed, for the screen."""
    target = switch_target(installed, requested_profile)
    changes: list[str] = []
    to_compact = target.deployment_profile == "compact"
    if not to_compact:
        # AC17-R165: before the first Scalable Compose call, which requires it.
        gateway.restore_switch_worker_token(installed, log=log)
    gateway.prepare_core(target, log=log)
    # AC17-R168: the host trees are touched only where they are the data. Back from a
    # switch that left them in the Compact volume, they are no part of it: a directory
    # of that name is some other installation's, or nobody's.
    on_host = installed.data_placement == "host_directories"
    roots = gateway.record_switch_host_trees(installed, log=log) if to_compact and on_host else {}
    # Nothing has changed yet; everything the target's Compose requires must be there.
    gateway.check_switch_target_renders(target, log=log)
    gateway.remove_switch_target_leftovers(installed, target, log=log)
    if to_compact:
        if roots:
            changes.extend(gateway.normalize_switch_host_trees(target, roots, log=log))
        cli_config = installed
    else:
        cli_config = target
    try:
        if not to_compact:
            gateway.ensure_cli_runtime(target, log=log)
        gateway.write_switch_pending(
            ConfigurationPatch(
                deployment_profile=target.deployment_profile,
                data_placement=target.data_placement,
                worker_replicas=1 if target.deployment_profile == "compact" else None,
            )
        )
        log.write(f"Recorded the switch to {target.deployment_profile}; applying it.")
        gateway.apply_switch_in_cli(cli_config, log=log)
    except InstallerFailure:
        if not to_compact and gateway.unfinished_profile_switch() is None:
            # AC17-R166 acceptance: a Scalable CLI container was left running on the
            # Compact installation. Nothing is left to continue, so it goes; a switch
            # left unfinished keeps it, and a resume would start it again anyway.
            gateway.remove_switch_cli_runtime(target, log=log)
        raise
    if to_compact:
        # AC17-R142: on Compact the runtime owns the worker identity; the apply has
        # handed it the credential, and the installer's copy goes.
        gateway.drop_superseded_worker_credential(target, log=log)
    removed = gateway.remove_switch_source_containers(installed, target, log=log)
    if removed:
        changes.append(removed)
    changes.append(
        f"Switched from {installed.deployment_profile} to {target.deployment_profile}; "
        f"the data stayed where it was ({_placement_words(target.data_placement)})."
    )
    return tuple(changes)


def resume_switch(
    gateway: InstallerGateway,
    installed: InstallConfig,
    candidate: Mapping[str, Any],
    *,
    log: InstallerLog,
) -> tuple[str, ...]:
    """Continue the profile switch an earlier run left, from the apply's own journal."""
    scalable = (
        installed
        if installed.deployment_profile == "scalable"
        else InstallConfig.model_validate({**installed.model_dump(mode="json"), **candidate})
    )
    log.write("An earlier run left a profile switch unfinished; continuing it.")
    if installed.deployment_profile != "scalable":
        gateway.ensure_cli_runtime(scalable, log=log)  # the apply runs there
    gateway.apply_switch_in_cli(scalable, log=log)
    return (
        f"Completed the switch to {candidate.get('deployment_profile')} that an earlier run left.",
    )


def _placement_words(placement: str) -> str:
    return (
        "Scalable's host directories"
        if placement == "host_directories"
        else "the Compact data volume"
    )
