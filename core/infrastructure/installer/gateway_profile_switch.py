"""ProfileSwitch technical operations for the installer adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.contracts import (
    DeploymentConfig,
)
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.installer.gateway_support import (
    _CORE_START_SERVICES,
    _MESSAGE_RUNNING,
    _SWITCH_APPLY_TIMEOUT_SECONDS,
    _SWITCH_HOST_GROUP_MOUNT,
    _SWITCH_HOST_TREES,
    _SWITCH_TREE_MOUNT,
    _SWITCH_TREE_PROBE,
    _TOKEN_COMPOSE_ENV,
    _WORKER_TOKEN_ENV,
    _compose_ps_rows,
    _last_error_text,
    _read_env,
)

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class ProfileSwitchOperations:
    def unfinished_profile_switch(self: DockerInstallerGateway) -> dict[str, Any] | None:
        """The candidate of a profile switch an earlier run left unfinished, if any."""
        from core.application.lifecycle.contracts import LifecycleOperationKind
        from core.infrastructure.lifecycle.state_store import LifecycleStateStore

        for journal in LifecycleStateStore(self.repo_root).unfinished_journals():
            if journal.kind is not LifecycleOperationKind.CONFIG_APPLY:
                continue
            fields = journal.parameters.get("pending_fields") or []
            candidate = journal.parameters.get("candidate")
            if "deployment_profile" in fields and isinstance(candidate, Mapping):
                return dict(candidate)
        return None

    def record_switch_host_trees(
        self: DockerInstallerGateway, installed: InstallConfig, *, log: InstallerLog
    ) -> dict[str, str]:
        """Where Scalable's trees are, written absolute where the Compact overlay reads them.

        Relative values are relative to infra/docker, as Compose reads them; the
        Compact overlay requires each of them, and would name a missing one.
        """
        env_path = self._env_path(installed)
        environment = {
            **_read_env(self.state_store.root / _TOKEN_COMPOSE_ENV),
            **_read_env(env_path),
        }
        roots: dict[str, str] = {}
        recorded: dict[str, str] = {}
        settings: dict[str, str] = {}
        for tree, name, default, below in _SWITCH_HOST_TREES:
            if tree == "artifacts" and installed.file_storage != "local_fs":
                continue  # AC17-R169: in object storage they are no host tree
            raw = Path(str(environment.get(name) or default))
            base = raw if raw.is_absolute() else (self.repo_root / "infra/docker" / raw).resolve()
            recorded[name] = str(base)
            root = base / below if below else base
            roots[tree] = str(root)
            settings[tree] = name
        image = str(environment.get("FS_COMPACT_IMAGE") or "").strip()
        for tree, root in roots.items():
            self._refuse_unreal_switch_tree(tree, Path(root), image)
        self._refuse_shared_switch_trees(roots, settings)
        self.state_store.update_env_values(env_path, recorded)
        log.write(
            "Recorded the Scalable trees for Compact: " + json.dumps(recorded, sort_keys=True)
        )
        return roots

    def _refuse_unreal_switch_tree(
        self: DockerInstallerGateway, tree: str, root: Path, image: str
    ) -> None:
        """A real directory, not a link -- asked of Docker, which mounts it.

        AC17-R168: this container mounts the checkout only, and a tree may sit beside
        it, so a check here named a directory that is there "not a real directory".
        The parent is mounted read-only, so the entry itself is looked at.
        """
        outcome = self.process.run(
            [
                "docker", "run", "--rm", "--network", "none", "--user", "0:0",
                "--mount", f"type=bind,src={root.parent},dst={_SWITCH_TREE_PROBE},readonly",
                "--entrypoint", "sh", image,
                "-c", f'test -d "{_SWITCH_TREE_PROBE}/$1" && ! test -L "{_SWITCH_TREE_PROBE}/$1"',
                "probe", root.name,
            ],
            separate_stderr=True,
        )  # fmt: skip
        if outcome.returncode != 0:
            raise InstallerFailure(
                code="profile_switch_tree_missing",
                cause=f"The Scalable {tree} directory is not a real directory: {root}",
                remediation="Restore it, or correct its setting in .env.prod, then rerun.",
                exit_code=7,
                retryable=True,
                step="preflight",
            )

    def _refuse_shared_switch_trees(
        self: DockerInstallerGateway, roots: Mapping[str, str], settings: Mapping[str, str]
    ) -> None:
        """No tree another installation mounts: the normalization would change it there too.

        AC17-R168: a tree configured outside the checkout can be one directory for every
        installation beside this one. A container of another project that mounts the
        tree, or a part of it, is a neighbour; one that mounts a directory above it is
        not, nor is this project's own.
        """
        ids = self.process.run(["docker", "ps", "--all", "--quiet"], separate_stderr=True)
        own = self._project_identity()
        users: dict[str, set[str]] = {}
        if ids.returncode == 0 and ids.stdout.split():
            listing = self.process.run(
                [
                    "docker", "inspect", "--format",
                    '{{index .Config.Labels "com.docker.compose.project"}}\t{{.Name}}'
                    "\t{{json .Mounts}}",
                    *ids.stdout.split(),
                ],
                separate_stderr=True,
            )  # fmt: skip
            for line in listing.stdout.splitlines():
                project, _tab, rest = line.partition("\t")
                name, _tab, mounts = rest.partition("\t")
                if project == own:
                    continue
                try:
                    binds = [m for m in json.loads(mounts) or () if m.get("Type") == "bind"]
                except (ValueError, AttributeError):
                    continue
                for mount in binds:
                    source = Path(str(mount.get("Source") or "/"))
                    for tree, root in roots.items():
                        if Path(root) in (source, *source.parents):
                            users.setdefault(tree, set()).add(project or name.lstrip("/"))
        if users:
            named = "; ".join(
                f"{roots[tree]} is also mounted by {', '.join(sorted(found))}"
                for tree, found in sorted(users.items())
            )
            raise InstallerFailure(
                code="profile_switch_tree_shared",
                cause=f"Another installation uses a directory the switch would change: {named}.",
                remediation=(
                    "Nothing was changed. Give this installation its own directory -- "
                    + ", ".join(sorted(settings[tree] for tree in users))
                    + " in .env.prod -- then rerun ./install.sh."
                ),
                exit_code=7,
                retryable=False,
                step="preflight",
            )

    def normalize_switch_host_trees(
        self: DockerInstallerGateway,
        target: InstallConfig,
        roots: Mapping[str, str],
        *,
        log: InstallerLog,
    ) -> list[str]:
        """Step 3, as root in the target Compact image, over the host trees only."""
        image = str(_read_env(self._env_path(target)).get("FS_COMPACT_IMAGE") or "").strip()
        command = ["docker", "run", "--rm", "--network", "none", "--user", "0:0"]
        for tree, root in roots.items():
            command.extend(["--mount", f"type=bind,src={root},dst={_SWITCH_TREE_MOUNT}/{tree}"])
        command.extend(
            ["--mount", f"type=bind,src=/etc/group,dst={_SWITCH_HOST_GROUP_MOUNT},readonly"]
        )
        command.extend(["--entrypoint", "python", image])
        command.extend(
            [
                "-m",
                "core.infrastructure.runtime.compact.shared_trees",
                "--host-group-file",
                _SWITCH_HOST_GROUP_MOUNT,
                *(f"{tree}={_SWITCH_TREE_MOUNT}/{tree}" for tree in roots),
            ]
        )
        outcome = self.process.run(command, separate_stderr=True)
        log.write("Normalized the shared trees: " + (outcome.stdout or outcome.stderr).strip())
        if outcome.returncode != 0:
            detail = _last_error_text(outcome) or "the normalization did not finish"
            raise InstallerFailure(
                code="profile_switch_trees_refused",
                cause=f"The Scalable trees could not be given the shared groups: {detail}",
                remediation="Nothing was switched. Resolve the cause above, then rerun ./install.sh.",
                exit_code=7,
                retryable=True,
                step="switch",
            )
        reports = json.loads(outcome.stdout or "{}").get("trees") or []
        return [str(report.get("summary") or "") for report in reports if report.get("summary")]

    def restore_switch_worker_token(
        self: DockerInstallerGateway, installed: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Compact to Scalable: put the runtime's worker credential back in .env.prod.

        AC17-R165: on Compact the runtime owns the worker identity (AC17-R142 empties
        the installer's copy), and Scalable's compose requires the value with `:?` for
        every command -- the switch failed at its first Scalable call. The credential in
        the volume is the same machine identity, valid against the same database; it is
        read and written, never logged.
        """
        image = str(_read_env(self._env_path(installed)).get("FS_COMPACT_IMAGE") or "").strip()
        outcome = self.process.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={self._project_identity()}_fs-compact-data,dst=/data,readonly",
                "--entrypoint",
                "cat",
                image,
                "/data/state/worker-mcp-token",
            ],
            separate_stderr=True,
        )
        token = outcome.stdout.strip() if outcome.returncode == 0 else ""
        if not token:
            raise InstallerFailure(
                code="profile_switch_worker_identity_missing",
                cause=(
                    "The Compact runtime's worker credential could not be read from its "
                    "data volume, and Scalable's workers need it."
                ),
                remediation=(
                    "Nothing was switched. Start the Compact runtime once (./install.sh), so "
                    "it records its worker identity, then rerun the switch."
                ),
                exit_code=7,
                retryable=True,
                step="preflight",
            )
        self.state_store.update_env_values(self._env_path(installed), {_WORKER_TOKEN_ENV: token})
        log.write("Restored the Compact runtime's worker credential for Scalable's workers.")

    def check_switch_target_renders(
        self: DockerInstallerGateway, target: InstallConfig, *, log: InstallerLog
    ) -> None:
        """Every value the target profile's Compose requires is there, before any change.

        AC17-R170: rendered with what the apply will write for the target over the
        installed file -- the target profile's own values among it. Scalable to Compact
        refused on FS_WORKER_PROCESSES, which a Scalable .env.prod has no use for and
        the apply writes for Compact itself.
        """
        environment = self._switch_target_environment(target)
        command = [*self._compose_prefix(target), "config", "--quiet"]
        if environment is not None:
            # AC17-R174: exactly as the apply renders -- from its values alone, never
            # the installed file, which held what the apply then did not have.
            command[command.index("--env-file") + 1] = "/dev/null"
        outcome = self.process.run(command, separate_stderr=True, environment=environment)
        if outcome.returncode == 0:
            log.write(f"The {target.deployment_profile} configuration renders completely.")
            return
        missing = sorted(set(re.findall(r"required variable (\w+) is missing", outcome.stderr)))
        raise InstallerFailure(
            code="profile_switch_target_incomplete",
            cause=(
                f"The {target.deployment_profile} configuration cannot be rendered"
                + (f": it needs {', '.join(missing)}" if missing else f": {outcome.stderr.strip()}")
                + ", which this installation does not have yet."
            ),
            remediation="Nothing was switched. Report this with the installer log.",
            exit_code=7,
            retryable=False,
            step="preflight",
        )

    def _switch_target_environment(
        self: DockerInstallerGateway, target: InstallConfig
    ) -> dict[str, str] | None:
        """The managed values `config apply` will render for the target, not yet written."""
        active = self._configuration.read_active(required=False)
        if active is None:
            return None
        candidate = DeploymentConfig.model_validate(target.model_dump(mode="json"))
        try:
            _target, _path, managed = self._configuration.prepare_transition_environment(
                active, candidate
            )
        except LifecycleError as exc:
            raise InstallerFailure(
                code="profile_switch_target_incomplete",
                cause=f"The {target.deployment_profile} configuration cannot be prepared: "
                + exc.cause,
                remediation=exc.remediation or "Nothing was switched; resolve the cause and rerun.",
                exit_code=7,
                retryable=False,
                step="preflight",
            ) from exc
        return dict(managed)

    def remove_switch_cli_runtime(
        self: DockerInstallerGateway, scalable: InstallConfig, *, log: InstallerLog
    ) -> None:
        """The Scalable CLI container a failed Compact-to-Scalable switch started."""
        outcome = self.process.run([*self._compose_prefix(scalable), "rm", "-sf", "fs-cli"])
        log.write(
            "Removed the Scalable CLI container the failed switch started."
            if outcome.returncode == 0
            else "Could not remove the Scalable CLI container the failed switch started."
        )

    def remove_switch_source_containers(
        self: DockerInstallerGateway,
        source: InstallConfig,
        target: InstallConfig,
        *,
        log: InstallerLog,
    ) -> str:
        """After a switch, the source profile's containers the target does not run.

        Live on fs-ac19-bench: after Scalable to Compact the Scalable fs-cli -- a
        management container with the Docker socket -- still ran on the Compact
        installation, and the Scalable services were left exited; after Compact to
        Scalable the exited compact-runtime made every Compose command warn of orphans.
        Found by their Compose labels, not through the source's Compose files, which
        the switched .env.prod no longer renders; removed without -v, so every volume
        stays. The database, the broker and the ingress run on both profiles and stay.
        """
        leaving = self._profile_only_services(source, target)
        if source.deployment_profile == "scalable":
            leaving.add("fs-cli")  # the apply ran there; Compact runs no CLI container
        ids = self._project_container_ids(leaving, stopped_only=False)
        if not ids or not self._remove_containers(ids, source.deployment_profile, log=log):
            return ""
        log.write(
            f"Removed the {source.deployment_profile} containers the switch left: "
            + ", ".join(sorted(leaving))
            + " (volumes kept)."
        )
        return f"Removed the {source.deployment_profile} containers ({len(ids)}); no volume."

    def remove_switch_target_leftovers(
        self: DockerInstallerGateway,
        installed: InstallConfig,
        target: InstallConfig,
        *,
        log: InstallerLog,
    ) -> None:
        """Before a switch, the target profile's containers an earlier switch left stopped.

        AC17-R171: a release that stopped the source containers without removing them
        left, after Compact to Scalable, a stopped compact-runtime built at that
        switch's revision; the way back recorded it as a container the apply had to
        keep, and after any update in between the apply refused on its image. The
        apply creates these services afresh for the target; a stopped one is only a
        leftover. A running one is not touched.
        """
        ids = self._project_container_ids(
            self._profile_only_services(target, installed), stopped_only=True
        )
        if ids and self._remove_containers(ids, target.deployment_profile, log=log):
            log.write(
                f"Removed {len(ids)} stopped {target.deployment_profile} containers an earlier "
                "switch left (volumes kept)."
            )

    def _profile_only_services(
        self: DockerInstallerGateway, config: InstallConfig, other: InstallConfig
    ) -> set[str]:
        return set(self._canonical_target(config).runtime_services) - set(
            self._canonical_target(other).runtime_services
        )

    def _project_container_ids(
        self: DockerInstallerGateway, services: set[str], *, stopped_only: bool
    ) -> list[str]:
        states = ("--filter", "status=exited", "--filter", "status=created") if stopped_only else ()
        ids: list[str] = []
        for service in sorted(services):
            outcome = self.process.run(
                [
                    "docker", "ps", "--all", "--quiet",
                    "--filter", f"label=com.docker.compose.project={self._project_identity()}",
                    "--filter", f"label=com.docker.compose.service={service}",
                    *states,
                ],
                separate_stderr=True,
            )  # fmt: skip
            ids.extend(outcome.stdout.split() if outcome.returncode == 0 else ())
        return ids

    def _remove_containers(
        self: DockerInstallerGateway, ids: list[str], profile: str, *, log: InstallerLog
    ) -> bool:
        outcome = self.process.run(["docker", "rm", "-f", *ids], separate_stderr=True)
        if outcome.returncode == 0:
            return True
        # Only clutter is left, and it is named; nothing the switch needs.
        notice = (
            f"{len(ids)} {profile} containers could not be removed: "
            f"{_last_error_text(outcome)}. Remove them with docker rm -f {' '.join(ids)}."
        )
        self.notices.append(notice)
        log.write(notice)
        return False

    def write_switch_pending(self: DockerInstallerGateway, patch: Any) -> None:
        self._configuration.store.write_pending(patch)

    def apply_switch_in_cli(
        self: DockerInstallerGateway, scalable: InstallConfig, *, log: InstallerLog
    ) -> None:
        """`config apply` in the Scalable CLI container, with the lifecycle lock lent."""
        context = self._cli_runtime_context(scalable, log=log)
        command = [
            *context.compose_prefix,
            "exec",
            "-T",
            "fs-cli",
            "python",
            "-m",
            "core.entrypoints.cli.lifecycle_click_app",
            "config",
            "apply",
            "--confirm",
        ]
        log.write(_MESSAGE_RUNNING + " ".join(command))
        with self.state_store.lifecycle_lock_lent():
            outcome = self.process.run(
                command,
                cwd=self.repo_root,
                environment=context.environment,
                output_sink=log.write,
                timeout_seconds=_SWITCH_APPLY_TIMEOUT_SECONDS,
            )
        if outcome.returncode != 0:
            raise InstallerFailure(
                code="profile_switch_apply_failed",
                cause="The profile switch did not complete: "
                + (_last_error_text(outcome) or "see the log"),
                remediation=(
                    "The switch is journaled and rolled back where it could be. Rerun "
                    "./install.sh to continue it; ./flow-steward status names the operation."
                ),
                exit_code=8,
                retryable=True,
                step="switch",
            )

    def start_stopped_services(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> str:
        """AC17-R162: bring back the services a `stack stop` (or a reboot) left down.

        The already-installed path only checked health, so ./install.sh after the
        runbook's stop failed with "fs-app: missing" and left the site down. Only what
        is not running is started, through the same start as an install, so a running
        stack is not touched; health is still verified afterwards.
        """
        outcome = self._checked(
            [*self._compose_prefix(config), "ps", "--all", "--format", "json"],
            log=log,
            code="container_unhealthy",
            remediation="Inspect the core containers in the installer log and rerun ./install.sh.",
            exit_code=8,
            separate_stderr=True,
        )
        rows = _compose_ps_rows(outcome.stdout)
        states: dict[str, set[str]] = {}
        for row in rows:
            service = str(row.get("Service") or row.get("service") or "")
            states.setdefault(service, set()).add(
                str(row.get("State") or row.get("state") or "").lower()
            )
        # A service is stopped when none of its containers runs, and also when one of
        # them does not -- a stopped fs-worker replica (C3 acceptance: it came back, and
        # nothing on the screen said so).
        stopped = sorted(
            service
            for service in self._health_expected_services(config)
            if states.get(service, set()) - {"running"}
            or "running" not in states.get(service, set())
        )
        if not stopped:
            return ""
        log.write("Starting the stopped services: " + ", ".join(stopped) + ".")
        if set(stopped) & _CORE_START_SERVICES:
            self.start_core(config, log=log)
        if set(stopped) - _CORE_START_SERVICES:
            self.start_runtime(config, log=log)
        return "Started the stopped services: " + ", ".join(stopped) + "."
