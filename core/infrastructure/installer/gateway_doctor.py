"""Doctor-specific technical operations for the installer adapter."""

from __future__ import annotations

import json
import ssl
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.application.installer.contracts import InstallConfig
from core.application.installer.ports import InstallerLog
from core.infrastructure.installer.gateway_support import (
    _BOOTSTRAP_HOST_ROUTE,
    _COMPACT_HEALTH_DETAILS,
    _FORMAT_IMAGE_ID,
    _FULL_GIT_REVISION,
    _HOSTS_FILE,
    _MESSAGE_RUNNING,
    _RUNTIME_IMAGES,
    _SWITCH_APPLY_TIMEOUT_SECONDS,
    _SWITCH_HOST_GROUP_MOUNT,
    _SWITCH_HOST_TREES,
    _SWITCH_TREE_MOUNT,
    _TOKEN_HEAD_COMMIT,
    _compose_ps_rows,
    _hosts_file_address,
    _is_ip_address,
    _last_error_text,
    _read_env,
)
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.lifecycle import docker_runtime_support

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway

_DOCTOR_SOURCE_MOUNT = "/run/flow-steward-source"
_FORMAT_CONTAINER_IMAGE = "{{.Image}}"
_JOB_QUEUE = "platform.projects.ready"
_MANAGEMENT_FORMAT = (
    '{{.Id}}\\t{{index .Config.Labels "com.flow-steward.management"}}'
    '\\t{{index .Config.Labels "com.flow-steward.installation"}}'
    '\\t{{index .Config.Labels "com.flow-steward.host-pid"}}'
    "\\t{{json .Config.Cmd}}\\t{{range .Mounts}}{{.Source}}|{{end}}"
)
_READY_WORKER_STATES = frozenset({"registered", "active", "maintenance_paused"})
_STALE_GRAPHS_QUERY = (
    "SELECT name FROM ag_catalog.ag_graph "
    "WHERE graphid IS DISTINCT FROM namespace::oid ORDER BY name"
)


class DoctorOperations:
    def run_in_scalable_cli(
        self: DockerInstallerGateway,
        scalable: InstallConfig,
        arguments: Sequence[str],
        *,
        log: InstallerLog,
    ) -> ProcessOutcome:
        """A lifecycle command in the Scalable CLI container, with the lifecycle lock lent."""
        context = self._cli_runtime_context(scalable, log=log)
        command = [
            *context.compose_prefix,
            "exec",
            "-T",
            "fs-cli",
            "python",
            "-m",
            "core.entrypoints.cli.lifecycle_click_app",
            *arguments,
        ]
        log.write(_MESSAGE_RUNNING + " ".join(command))
        with self.state_store.lifecycle_lock_lent():
            return self.process.run(
                command,
                cwd=self.repo_root,
                environment=context.environment,
                output_sink=log.write,
                timeout_seconds=_SWITCH_APPLY_TIMEOUT_SECONDS,
            )

    def run_compact_platform_command(
        self: DockerInstallerGateway,
        compact: InstallConfig,
        arguments: Sequence[str],
        *,
        log: InstallerLog,
    ) -> ProcessOutcome:
        """A Compact platform command, the way ./flow-steward runs it (a one-shot fs-cli)."""
        command = [*self._compose_prefix(compact), "run", "--rm", "--no-deps", "-T", "fs-cli"]
        command.extend(arguments)
        log.write(_MESSAGE_RUNNING + " ".join(command))
        with self.state_store.lifecycle_lock_lent():
            return self.process.run(command, cwd=self.repo_root, output_sink=log.write)

    def read_compact_runtime_file(
        self: DockerInstallerGateway, compact: InstallConfig, path: str
    ) -> str | None:
        """A file inside the running Compact runtime; None when it does not exist there."""
        outcome = self.process.run(
                [*self._compose_prefix(compact), "exec", "-T", "compact-runtime", "sh", "-c",
                 'test -e "$1" || exit 3; cat -- "$1"', "read", path],
                separate_stderr=True,
            )  # fmt: skip
        if outcome.returncode == 3:
            return None
        if outcome.returncode != 0:
            raise OSError(_last_error_text(outcome) or "the Compact runtime could not be read")
        return outcome.stdout

    def shared_tree_roots(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> tuple[str, dict[str, str]] | None:
        """Where the trees the shared model governs are: in the Compact volume, or bound
        from the host. None where no model applies -- Scalable on its own directories,
        which belong to the deployment owner alone."""
        local_artifacts = config.file_storage == "local_fs"
        if config.data_placement == "compact_volume":
            trees = {"extensions": "/data/extensions"}
            if local_artifacts:
                trees["artifacts"] = "/data/artifacts"
            return "volume", trees
        if config.deployment_profile != "compact":
            return None
        environment = _read_env(self._env_path(config))
        roots: dict[str, str] = {}
        for tree, name, default, below in _SWITCH_HOST_TREES:
            if tree == "artifacts" and not local_artifacts:
                continue
            raw = Path(str(environment.get(name) or default))
            base = raw if raw.is_absolute() else (self.repo_root / "infra/docker" / raw).resolve()
            roots[tree] = str(base / below if below else base)
        return "host", roots

    def shared_tree_reports(
        self: DockerInstallerGateway, config: InstallConfig, *, repair: bool = False
    ) -> list[dict[str, Any]] | None:
        """The shared trees as the normalizer sees them; `repair` normalizes them (a full
        walk). Run as root in the Compact image, without network; read-only unless
        repairing."""
        placement = self.shared_tree_roots(config)
        if placement is None:
            return None
        where, roots = placement
        image = str(_read_env(self._env_path(config)).get("FS_COMPACT_IMAGE") or "").strip()
        if not image:
            raise OSError("FS_COMPACT_IMAGE is not recorded, and the check runs in that image")
        readonly = "" if repair else ",readonly"
        command = ["docker", "run", "--rm", "--network", "none", "--user", "0:0"]
        if where == "volume":
            volume = f"{self._project_identity()}_fs-compact-data"
            # `docker run` would create a missing volume, empty, under that name.
            if self.process.run(["docker", "volume", "inspect", volume]).returncode != 0:
                raise OSError(f"the Compact data volume {volume} does not exist")
            command.extend(["--mount", f"type=volume,src={volume},dst=/data{readonly}"])
            arguments = [f"{tree}={root}" for tree, root in roots.items()]
        else:
            for tree, root in roots.items():
                command.extend(
                    ["--mount", f"type=bind,src={root},dst={_SWITCH_TREE_MOUNT}/{tree}{readonly}"]
                )
            arguments = [f"{tree}={_SWITCH_TREE_MOUNT}/{tree}" for tree in roots]
        if repair:
            command.extend(
                ["--mount", f"type=bind,src=/etc/group,dst={_SWITCH_HOST_GROUP_MOUNT},readonly"]
            )
            flags = ["--full", "--host-group-file", _SWITCH_HOST_GROUP_MOUNT]
        else:
            flags = ["--inspect"]
        # The checkout's own code, read-only, in the image's Python: the helper is always
        # the doctor's revision, whichever release built the image.
        command.extend(
            ["--mount", f"type=bind,src={self.repo_root},dst={_DOCTOR_SOURCE_MOUNT},readonly"]
        )
        command.extend(["--workdir", _DOCTOR_SOURCE_MOUNT, "--entrypoint", "python", image])
        command.extend(["-m", "core.infrastructure.runtime.compact.shared_trees", *flags])
        outcome = self.process.run([*command, *arguments], separate_stderr=True)
        if outcome.returncode != 0:
            raise OSError(_last_error_text(outcome) or "the shared trees could not be read")
        reports = list(json.loads(outcome.stdout or "{}").get("trees") or ())
        for report in reports:  # the operator's own paths, not the helper's mount points
            report["root"] = roots.get(str(report.get("tree")), report.get("root"))
        return reports

    def installed_runtime_revision(self: DockerInstallerGateway, config: InstallConfig) -> str:
        """The revision the installation's runtime was built from, asked of the image its
        container was created from -- running or stopped, never a record (doctor,
        principle 0). "" when there is no such container or it does not say."""
        service, _name = _RUNTIME_IMAGES[config.deployment_profile][0]
        container_ids = self._running_container_ids(service, stopped_too=True)
        if not container_ids:
            return ""
        inspected = self.process.run(
            ["docker", "container", "inspect", "--format", _FORMAT_CONTAINER_IMAGE, *container_ids]
        )
        images = {line.strip() for line in inspected.stdout.splitlines() if line.strip()}
        if inspected.returncode != 0 or len(images) != 1:
            return ""
        return self._image_revision_label(images.pop())

    def source_revision(self: DockerInstallerGateway) -> str:
        """The revision of this checkout, or "" when git cannot say."""
        outcome = self.process.run(
            ["git", "-C", str(self.repo_root), "rev-parse", _TOKEN_HEAD_COMMIT],
            separate_stderr=True,
        )
        revision = outcome.stdout.strip() if outcome.returncode == 0 else ""
        return revision if _FULL_GIT_REVISION.fullmatch(revision) else ""

    def stale_graphs(self: DockerInstallerGateway, config: InstallConfig) -> list[str]:
        """Graphs whose id no longer names their namespace (AC17-R164); reads only."""
        present = self._database_query(
            config, "SELECT to_regclass('ag_catalog.ag_graph') IS NOT NULL"
        )
        if present.strip() != "t":
            return []
        stale = self._database_query(config, _STALE_GRAPHS_QUERY)
        return [line for line in stale.splitlines() if line.strip()]

    def repair_graph_catalog(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """The graph catalogue repair db_restore.sh ends with: idempotent, one definition."""
        sql = (self.repo_root / "core/entrypoints/cli/scripts/age_catalog_repair.sql").read_text(
            encoding="utf-8"
        )
        self._database_query(config, sql)
        log.write("Ran the graph catalogue repair.")

    def temporary_container_names(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> tuple[docker_runtime_support.RecreateRepair, ...]:
        """W4: what finishes the recreates a kill left -- the containers on "<id>_<name>",
        and a stopped replaced container that still holds a name."""
        del config
        listing = self.process.run(
            list(docker_runtime_support.recreate_listing_command(self._project_identity()))
        )
        if listing.returncode != 0:
            raise OSError(_last_error_text(listing) or "docker ps did not answer")
        return docker_runtime_support.recreate_repairs(
            listing.stdout, project_name=self._project_identity()
        )

    def interrupted_update(self: DockerInstallerGateway, config: InstallConfig) -> str:
        """F1: an update killed part-way leaves no journal -- updates keep none -- but it
        leaves this fact: .env.prod already names the new runtime image, and the service's
        containers (running or not) were created from another, or from two. "" when every
        runtime service is on the image recorded for it."""
        persisted = _read_env(self._env_path(config))
        behind = []
        for service, name in _RUNTIME_IMAGES[config.deployment_profile]:
            recorded = self._image_id(str(persisted.get(name) or "").strip())
            container_ids = self._running_container_ids(service, stopped_too=True)
            if not recorded or not container_ids:
                continue  # a missing image or no container at all is another check's
            inspected = self.process.run(
                [
                    "docker",
                    "container",
                    "inspect",
                    "--format",
                    _FORMAT_CONTAINER_IMAGE,
                    *container_ids,
                ]
            )
            images = {line.strip() for line in inspected.stdout.splitlines() if line.strip()}
            if inspected.returncode == 0 and images != {recorded}:
                behind.append(service)
        if not behind:
            return ""
        return (
            ", ".join(behind)
            + (" is" if len(behind) == 1 else " are")
            + " not on the image .env.prod records for it"
        )

    def read_compact_volume(
        self: DockerInstallerGateway, config: InstallConfig, script: str, *arguments: str
    ) -> str:
        """A read-only snippet over the Compact data volume while the runtime is down: the
        recorded Compact image's Python, no network, the volume mounted read-only."""
        image = str(_read_env(self._env_path(config)).get("FS_COMPACT_IMAGE") or "").strip()
        if not image:
            raise OSError("FS_COMPACT_IMAGE is not recorded")
        volume = f"{self._project_identity()}_fs-compact-data"
        # `docker run` would create a missing volume, empty, under that name.
        if self.process.run(["docker", "volume", "inspect", volume]).returncode != 0:
            raise OSError(f"the Compact data volume {volume} does not exist")
        outcome = self.process.run(
                ["docker", "run", "--rm", "--network", "none", "--user", "0:0",
                 "--mount", f"type=volume,src={volume},dst=/data,readonly",
                 "--entrypoint", "python", image, "-c", script, *arguments],
                separate_stderr=True,
            )  # fmt: skip
        if outcome.returncode != 0:
            raise OSError(_last_error_text(outcome) or "the Compact data volume could not be read")
        return outcome.stdout

    def management_containers(self: DockerInstallerGateway) -> list[dict[str, str]]:
        """This installation's running installer and doctor containers (orphan_installer):
        labelled by install.sh, or -- started before the labels -- running its installer
        module with this checkout mounted."""
        listed = self.process.run(["docker", "ps", "-q", "--no-trunc"])
        container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if listed.returncode != 0 or not container_ids:
            return []
        inspected = self.process.run(
            ["docker", "container", "inspect", "--format", _MANAGEMENT_FORMAT, *container_ids],
            separate_stderr=True,
        )
        root = str(self.repo_root)
        found = []
        for line in inspected.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            container_id, module, installation, host_pid, command, mounts = fields
            if installation and installation != root:
                continue  # another installation's, on a shared host
            if not installation:
                module = next(
                    (
                        name
                        for name in ("installer", "doctor")
                        if f"core.entrypoints.cli.{name}" in command
                    ),
                    "",
                )
                if not module or root not in mounts.split("|"):
                    continue
            found.append({"id": container_id, "module": module, "host_pid": host_pid})
        return found

    def stop_container(self: DockerInstallerGateway, container_id: str) -> bool:
        return self.process.run(["docker", "stop", container_id]).returncode == 0

    def restore_container_names(self: DockerInstallerGateway, config: InstallConfig) -> None:
        """The rename lifecycle operations already make after a recreate (W4)."""
        del config
        docker_runtime_support.restore_temporary_names(
            lambda command: self.process.run(list(command)),
            project_name=self._project_identity(),
        )

    def cli_runtime_mismatch(self: DockerInstallerGateway, config: InstallConfig) -> str:
        """Why the retained fs-cli is not the one this installation expects; "" when it
        is. Scalable only: Compact's fs-cli is a one-shot."""
        if config.deployment_profile == "compact":
            return ""
        context = self._cli_runtime_context(config, log=None)
        revision = self._cli_image_revision(context, log=None)
        if revision != context.build_revision:
            return (
                f"the fs-cli image is at {revision[:12] or 'an unknown revision'}, "
                f"the installation at {context.build_revision[:12]}"
            )
        image_id = self._cli_image_id(context, log=None)
        matches, mismatch = self._cli_runtime_matches(context, expected_image_id=image_id, log=None)
        return "" if matches else str(mismatch or "fs-cli does not match its runtime identity")

    def runtime_python(
        self: DockerInstallerGateway, config: InstallConfig, script: str, *arguments: str
    ) -> str:
        """A read-only snippet in the running runtime service -- the installed image's own
        code, which knows its release's migrations and backup format. Its stdout."""
        service, _name = _RUNTIME_IMAGES[config.deployment_profile][0]
        outcome = self.process.run(
                [*self._compose_prefix(config), "exec", "-T", service, "python", "-c", script,
                 *arguments],
                separate_stderr=True,
            )  # fmt: skip
        if outcome.returncode != 0:
            raise OSError(_last_error_text(outcome) or f"{service} could not run the check")
        return outcome.stdout

    def certificate_not_after(self: DockerInstallerGateway, path: Path) -> datetime | None:
        """When a PEM certificate expires, or None when it cannot be read."""
        outcome = self.process.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(path)], separate_stderr=True
        )
        if outcome.returncode != 0:
            return None
        value = outcome.stdout.strip().partition("=")[2]
        try:
            return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        except ValueError:
            return None

    def public_route_problem(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> tuple[str, str]:
        """One request to the public /health, the way the install verifies it; ("", "")
        when it answers."""
        url = self.product_url(config) + "/health"
        try:
            with self._open_public_probe(
                url, config=config, context=self._public_tls_context(config)
            ) as response:
                if int(response.status) >= 400:
                    raise OSError(f"HTTP {response.status}")
        except ssl.SSLCertVerificationError as exc:
            failure = self._public_certificate_failure(exc)
            return failure.cause, failure.remediation
        except OSError as exc:
            reason = getattr(exc, "reason", exc)
            return (
                f"The public URL {url} does not answer: {reason}.",
                f"Check DNS, the firewall and any proxy in front of {self.product_url(config)};"
                " ./flow-steward status shows the stack itself.",
            )
        return "", ""

    def host_gateway_problem(self: DockerInstallerGateway) -> tuple[str, str]:
        """What Docker mapped host.docker.internal to in this container, when it is not an
        address (the server incident: `invalid IP`); ("", "") otherwise."""
        mapped_to = _hosts_file_address(_HOSTS_FILE, _BOOTSTRAP_HOST_ROUTE)
        if mapped_to is None or _is_ip_address(mapped_to):
            return "", ""
        return self._host_route_unresolved(
            _BOOTSTRAP_HOST_ROUTE, OSError("not an address"), mapped_to=mapped_to
        )

    def queue_consumers(self: DockerInstallerGateway, config: InstallConfig) -> int | None:
        """Consumers on the job queue as RabbitMQ counts them; None before it exists."""
        outcome = self.process.run(
                [*self._compose_prefix(config), "exec", "-T", "rabbitmq-fs", "rabbitmqctl", "-q",
                 "list_queues", "name", "consumers"],
                separate_stderr=True,
            )  # fmt: skip
        if outcome.returncode != 0:
            raise OSError(_last_error_text(outcome) or "RabbitMQ could not list its queues")
        for line in outcome.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == _JOB_QUEUE and fields[1].isdigit():
                return int(fields[1])
        return None

    def running_worker_count(self: DockerInstallerGateway, config: InstallConfig) -> int:
        """Workers that run: fs-worker replicas, or the Compact runtime's ready workers."""
        if config.deployment_profile != "compact":
            return len(self._running_container_ids("fs-worker"))
        answer = self.runtime_python(config, _COMPACT_HEALTH_DETAILS)
        details = json.loads(answer or "{}")
        states = details.get("worker_states") if isinstance(details, dict) else None
        return sum(1 for state in (states or {}).values() if state in _READY_WORKER_STATES)

    def missing_recorded_images(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> list[tuple[str, str]]:
        """AC17-R177: the images .env.prod names that this host no longer has, as (name,
        value) -- after `docker image prune -a` an update failed with "pull access denied
        for sha256"."""
        persisted = _read_env(self._env_path(config))
        names = [name for _service, name in _RUNTIME_IMAGES[config.deployment_profile]]
        names.append("POSTGRES_IMAGE")
        if config.deployment_profile != "compact":
            names.append("FS_CLI_IMAGE")
        missing = []
        for name in names:
            value = str(persisted.get(name) or "").strip()
            if not value:
                continue
            inspected = self.process.run(
                ["docker", "image", "inspect", "--format", _FORMAT_IMAGE_ID, value]
            )
            if inspected.returncode != 0:
                missing.append((name, value))
        return missing

    def _database_query(self: DockerInstallerGateway, config: InstallConfig, sql: str) -> str:
        outcome = self.process.run(
                [*self._compose_prefix(config), "exec", "-T", "pg-fs", "sh", "-c",
                 'exec psql -v ON_ERROR_STOP=1 -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                input_text=sql,
                separate_stderr=True,
            )  # fmt: skip
        if outcome.returncode != 0:
            raise OSError(_last_error_text(outcome) or "the database could not be queried")
        return outcome.stdout

    def stopped_services(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> list[str]:
        """The services that should run and do not; reads only (also doctor's stack_stopped)."""
        outcome = self._checked(
            [*self._compose_prefix(config), "ps", "--all", "--format", "json"],
            log=log,
            code="container_unhealthy",
            remediation="Inspect the core containers in the installer log and rerun ./install.sh.",
            exit_code=8,
            separate_stderr=True,  # F3: Compose warns on stderr and still answers
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
        return sorted(
            service
            for service in self._health_expected_services(config)
            if states.get(service, set()) - {"running"}
            or "running" not in states.get(service, set())
        )
