"""HostPreflight technical operations for the installer adapter."""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

from core.application.installer.contracts import (
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.infrastructure.installer.gateway_support import (
    _BOOTSTRAP_DOCKER_MODE,
    _BOOTSTRAP_HOST_ROUTE,
    _DEFAULT_REDIRECT_HTTP_PORT,
    _HOSTS_FILE,
    _IMAGE_ID,
    _MESSAGE_VERIFY_LOCAL_DOCKER_ACCESS_AND_RERUN_INSTALL_SH,
    _MINIMUM_DISK_BYTES,
    _SUPPORTED_ARCHITECTURES,
    _hosts_file_address,
    _is_ip_address,
    _port_is_available,
    _publishes_no_host_port,
    _read_env,
    _redirect_http_port,
)
from core.infrastructure.runtime_containers import operator_container

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class HostPreflightOperations:
    def preflight(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        self._preflight(config, log=log, installed=False)

    def update_preflight(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        """preflight, for an installation that is already running.

        AC17-R141 acceptance. The update ran the fresh-install preflight, and was refused
        on both stands before it reached anything else. On Compact the ports were the
        installation's own, held by its own Traefik; on Scalable the probe asked about
        8080, a port this installation never ran on, while it served on 8592.

        An update rewrites no configuration, so the ports it binds are the ones Compose
        resolves from the installed environment. Those are what is probed here, and a
        port this installation's own project already publishes is not a conflict.
        """
        self._preflight(config, log=log, installed=True)
        self.refuse_unrecorded_worker_scale(config, log=log)

    def _preflight(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog, installed: bool
    ) -> None:
        self._require_compact_image_input(config)
        known_images = {**_read_env(self._env_path(config)), **self._environment}
        self._exact_prebuilt_image_refs(config, known_images)
        pending_release = self._official_pending_release(config)
        docker_host = str(self._environment.get("DOCKER_HOST") or "").strip().lower()
        docker_context = str(self._environment.get("DOCKER_CONTEXT") or "").strip().lower()
        if docker_host.startswith(("tcp://", "ssh://")) or docker_context not in {
            "",
            "default",
            "desktop-linux",
        }:
            raise InstallerFailure(
                code="remote_docker_unsupported",
                cause="The active Docker endpoint is remote from this repository.",
                remediation="Use the local Docker Engine or Docker Desktop context and rerun ./install.sh.",
                exit_code=7,
                retryable=True,
                step="preflight",
            )
        if not self._docker_cli_available():
            raise InstallerFailure(
                code="docker_cli_missing",
                cause="The Docker CLI is not installed or is not on PATH.",
                remediation=(
                    "Install Docker Engine (Linux/server): https://docs.docker.com/engine/install/ "
                    "or Docker Desktop (Mac/Windows/Linux desktop, includes Compose): "
                    "https://docs.docker.com/desktop/ then rerun ./install.sh."
                ),
                exit_code=7,
                retryable=True,
                step="preflight",
            )
        self._checked(
            ["docker", "info", "--format", "{{json .}}"],
            log=log,
            code="docker_daemon_unavailable",
            remediation="Start Docker and verify permission to use its socket, then rerun ./install.sh.",
            exit_code=7,
        )
        self._checked(
            ["docker", "compose", "version", "--short"],
            log=log,
            code="docker_compose_unavailable",
            remediation=(
                "Install Docker Compose v2: https://docs.docker.com/compose/install/linux/ "
                "or Docker Desktop (includes Compose): https://docs.docker.com/desktop/ "
                "then rerun ./install.sh."
            ),
            exit_code=7,
        )
        # Every prepare_core path pins image provenance to the checkout's HEAD, so a
        # revision that cannot be read is a precondition failure, not a build failure.
        source_revision = self._require_resolvable_source_revision(log=log)
        if pending_release is not None and source_revision != pending_release.commit_sha:
            raise InstallerFailure(
                code="official_release_checkout_mismatch",
                cause="The installer checkout does not match the pending official release commit.",
                remediation="Resume the same official release handoff from its exact Git tag.",
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        architecture = self._architecture().lower()
        if architecture not in _SUPPORTED_ARCHITECTURES:
            raise InstallerFailure(
                code="unsupported_architecture",
                cause=f"Unsupported host architecture: {architecture}.",
                remediation="Use a supported amd64 or arm64 host.",
                exit_code=7,
                retryable=False,
                step="preflight",
            )
        if self._disk_free_bytes(self.repo_root) < _MINIMUM_DISK_BYTES:
            raise InstallerFailure(
                code="insufficient_disk_space",
                cause="Less than 4 GiB of free disk space is available.",
                remediation="Free at least 4 GiB for images, volumes, and build output.",
                exit_code=4,
                step="preflight",
            )
        if _publishes_no_host_port(config):
            # The public port belongs to an external component in these modes and the
            # selected overlay declares no `ports:` at all, so a bound public port is a
            # required condition rather than a conflict. The precondition that does matter
            # is that the network the operator named actually carries that component.
            if config.ingress_mode == "external_traefik":
                self._verify_external_ingress_network(config)
            self._verify_repository_materialized()
            return
        port_host = (
            config.reverse_proxy_backend_host
            if config.tls_mode == "reverse_proxy"
            else config.public_host
        )
        port = (
            config.reverse_proxy_backend_port
            if config.tls_mode == "reverse_proxy"
            else config.public_port
        )
        host_ports = (
            self._installed_host_ports(config, port, log=log)
            if installed
            else self._published_host_ports(config, port)
        )
        for probe_port, knob in host_ports:
            if self._port_available is None:
                port_available = self._host_port_is_available(
                    port_host,
                    probe_port,
                    bind_all_interfaces=config.tls_mode != "reverse_proxy",
                    include_udp=config.tls_mode != "reverse_proxy",
                    log=log,
                )
            else:
                port_available = self._port_available(port_host, probe_port)
            if not port_available and not self._port_owned_by_project(probe_port):
                raise InstallerFailure(
                    code="port_conflict",
                    cause=f"Port {probe_port} is already in use.",
                    remediation=(
                        f"Stop the conflicting service or select another installer port ({knob})."
                    ),
                    exit_code=4,
                    step="preflight",
                )
        self._verify_repository_materialized()

    @staticmethod
    def _operator_container(service: str, config: InstallConfig) -> str:
        """AC17-R106: name a container this installation's profile actually runs."""
        return operator_container(service, deployment_profile=config.deployment_profile)

    def _resolved_redirect_http_port(self: DockerInstallerGateway, config: InstallConfig) -> int:
        """The redirect port this installation will actually bind.

        AC17-R92 made the port an answer instead of a variable the operator had to
        know to export, and taught the preflight probe that the answer wins over the
        ambient variable. The env file was not taught the same thing: it kept writing
        `_redirect_http_port(self._environment)`, so `--redirect-http-port 8472` was
        probed on 8472, recorded as 8080, and bound on 8080 -- accepted, then fatal,
        with `.env.prod` still claiming 8080 afterwards. AC17-R103 gives both the one
        answer instead of leaving the resolution written twice.

        The ambient variable still wins where the answer was left at its default,
        which is the one case the two cannot be told apart from here.
        """
        redirect_port = int(config.redirect_http_port)
        if redirect_port == _DEFAULT_REDIRECT_HTTP_PORT:
            return _redirect_http_port(self._environment)
        return redirect_port

    def _installed_host_ports(
        self: DockerInstallerGateway, config: InstallConfig, public_port: int, *, log: InstallerLog
    ) -> tuple[tuple[int, str], ...]:
        """The host ports an update will bind: what Compose resolves for this installation.

        Resolved the way Compose resolves them -- the process environment, then the
        installed env file, then the file's own default -- because that, and not the
        recorded lifecycle configuration, is what the containers will publish. The two
        can disagree on an installation that predates AC17-R103, which recorded one
        redirect port and bound another. That disagreement is named in the log rather
        than silently resolved: probing the recorded value would check a port the
        installation has never used.
        """
        persisted = _read_env(self._env_path(config))

        def resolved(name: str, default: int) -> int:
            raw = str(self._environment.get(name) or persisted.get(name) or "").strip()
            return int(raw) if raw.isdigit() and 1 <= int(raw) <= 65535 else default

        recorded = self._published_host_ports(config, public_port)
        app_port = resolved("APP_PORT", public_port)
        ports: list[tuple[int, str]] = [(app_port, "APP_PORT in .env.prod")]
        if config.ingress_mode == "bundled_traefik":
            redirect_port = resolved("HTTP_PORT", _DEFAULT_REDIRECT_HTTP_PORT)
            if redirect_port != app_port:
                ports.append((redirect_port, "HTTP_PORT in .env.prod"))
        installed = {port for port, _knob in ports}
        drifted = sorted({port for port, _knob in recorded} - installed)
        if drifted:
            log.write(
                "The recorded installation configuration names port(s) "
                f"{', '.join(map(str, drifted))}, but this installation publishes "
                f"{', '.join(str(port) for port, _knob in ports)} from its environment. "
                "The update keeps the ports the installation runs on."
            )
        return tuple(ports)

    def _published_host_ports(
        self: DockerInstallerGateway, config: InstallConfig, public_port: int
    ) -> tuple[tuple[int, str], ...]:
        """Every host port the selected overlay binds, with the knob that sets each.

        AC17-R82: bundled Traefik publishes two -- the HTTPS entrypoint on the public port
        the operator chose, and the plain-HTTP entrypoint that redirects to it. Only the
        first was ever asked about or probed, so an install that named a free public port
        still died on the second, after the network, both volumes and three containers had
        been created, with a raw Docker "Bind for 0.0.0.0:8080 failed" that named neither
        the redirect entrypoint nor the variable that moves it. Port 8080 is busy on a great
        many hosts, including any host already running an installation of this product.
        """
        ports: list[tuple[int, str]] = [(public_port, "--public-port")]
        if config.ingress_mode == "bundled_traefik":
            redirect_port = self._resolved_redirect_http_port(config)
            if redirect_port != public_port:
                ports.append((redirect_port, "--redirect-http-port"))
        return tuple(ports)

    def _verify_external_ingress_network(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> None:
        """Prove the declared external ingress network exists and carries a proxy."""
        network = str(config.traefik_network or "").strip()
        if not network:  # pragma: no cover - DeploymentConfig already requires it
            raise InstallerFailure(
                code="traefik_network_missing",
                cause="External Traefik ingress requires a network name.",
                remediation="Pass --traefik-network with the network your Traefik uses.",
                exit_code=4,
                step="preflight",
            )
        outcome = self.process.run(
            ["docker", "network", "inspect", network, "--format", "{{len .Containers}}"],
            cwd=self.repo_root,
        )
        if outcome.returncode != 0:
            raise InstallerFailure(
                code="traefik_network_missing",
                cause=f"The external Traefik network {network!r} does not exist on this host.",
                remediation=(
                    "Create that network or pass the one your Traefik already uses with "
                    "--traefik-network, then rerun ./install.sh."
                ),
                exit_code=4,
                step="preflight",
            )
        attached = str(outcome.stdout or "").strip()
        if not attached.isdigit() or int(attached) < 1:
            raise InstallerFailure(
                code="traefik_network_unattached",
                cause=(
                    f"No container is attached to the external Traefik network {network!r}, "
                    "so nothing would route the public host to this installation."
                ),
                remediation=(
                    "Start the external reverse proxy on that network, then rerun ./install.sh."
                ),
                exit_code=4,
                step="preflight",
            )

    def _verify_repository_materialized(self: DockerInstallerGateway) -> None:
        required = (
            self.repo_root / "infra/docker/docker-compose.yaml",
            self.repo_root / "infra/docker/docker-compose.prod.yaml",
            self.repo_root / "db/schema",
        )
        missing = [str(path) for path in required if not os.access(path, os.R_OK)]
        if missing:
            raise InstallerFailure(
                code="repository_unreadable",
                cause="Required repository paths are unreadable: " + ", ".join(missing),
                remediation="Restore repository files and read permissions, then rerun ./install.sh.",
                exit_code=5,
                step="preflight",
            )

    def _host_port_is_available(
        self: DockerInstallerGateway,
        host: str,
        port: int,
        *,
        bind_all_interfaces: bool,
        include_udp: bool,
        log: InstallerLog,
    ) -> bool:
        if self._environment.get("FS_INSTALLER_USE_HOST_PYTHON") != _BOOTSTRAP_DOCKER_MODE:
            return _port_is_available(host, port)

        log.write(f"Checking Docker host route for an active listener on port {port}.")
        if self._host_route_has_listener(port, log=log):
            return False

        image_id = self._bootstrap_probe_image_id()

        bind_host = ""
        if not bind_all_interfaces:
            bind_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        publish_prefix = f"{bind_host}:" if bind_host else ""
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--publish",
            f"{publish_prefix}{port}:1/tcp",
        ]
        if include_udp:
            command.extend(("--publish", f"{publish_prefix}{port}:1/udp"))
        command.extend(("--entrypoint", "python3", image_id, "-c", "pass"))
        log.write(f"Checking Docker host port {port} with an ephemeral bind probe.")
        try:
            outcome = self.process.run(
                command,
                cwd=self.repo_root,
                timeout_seconds=10,
                separate_stderr=True,
            )
        except OSError as exc:
            raise InstallerFailure(
                code="host_port_bind_probe_failed",
                cause=f"The Docker host bind probe failed: {exc}",
                remediation=_MESSAGE_VERIFY_LOCAL_DOCKER_ACCESS_AND_RERUN_INSTALL_SH,
                exit_code=7,
                retryable=True,
                step="preflight",
            ) from exc
        if outcome.returncode == 0:
            return True

        detail = (outcome.stderr or outcome.stdout).strip()
        normalized = detail.lower()
        if any(
            marker in normalized
            for marker in (
                "address already in use",
                "port is already allocated",
                "ports are not available",
            )
        ):
            return False
        raise InstallerFailure(
            code="host_port_bind_probe_failed",
            cause=f"The Docker host bind probe failed: {detail or outcome.returncode}",
            remediation=_MESSAGE_VERIFY_LOCAL_DOCKER_ACCESS_AND_RERUN_INSTALL_SH,
            exit_code=7,
            retryable=True,
            step="preflight",
        )

    @staticmethod
    def _host_route_unresolved(
        route: str, exc: OSError, *, mapped_to: str | None = None
    ) -> tuple[str, str]:
        """Cause and remediation for a Docker host route that does not resolve.

        The auditor's case: after a daemon.json change without a restart, Docker wrote
        `invalid IP  host.docker.internal` into this container's /etc/hosts for
        `--add-host host.docker.internal:host-gateway`. The installer had been started
        the right way, and the old text said it probably had not. What Docker wrote is
        readable here, so the failure says which of the two it is.
        """
        if mapped_to is None:
            mapped_to = _hosts_file_address(_HOSTS_FILE, _BOOTSTRAP_HOST_ROUTE)
        if mapped_to is not None and not _is_ip_address(mapped_to):
            return (
                f"The installer could not resolve the Docker host route {route}: {exc}. "
                f"Docker mapped {_BOOTSTRAP_HOST_ROUTE} to {mapped_to!r} in this "
                "container's /etc/hosts: the daemon could not resolve host-gateway to an "
                "address.",
                "Check the Docker daemon configuration (host-gateway-ip, the default "
                "bridge) and restart dockerd, then rerun ./install.sh.",
            )
        return (
            f"The installer could not resolve the Docker host route {route}: {exc}. "
            "install.sh maps that name with "
            "--add-host host.docker.internal:host-gateway, so this usually means the "
            "installer was started another way.",
            "Run ./install.sh, which maps host.docker.internal to the Docker host "
            "gateway, and rerun the installation.",
        )

    @staticmethod
    def _host_route_has_listener(port: int, *, log: InstallerLog | None = None) -> bool:
        """Report whether something already answers on the host's port.

        AC17-R78. This is a fast pre-check; the authoritative answer is the
        `docker run --publish` bind probe below, which reports "address already in use" when
        the port is taken and succeeds when it is free. That probe binds on the host and needs
        no container-to-host traffic at all, so a firewall cannot mislead it.

        The meaning of a timeout here was inverted. On a hardened host -- `ufw` with
        `Chain INPUT (policy DROP)`, which is ordinary Ubuntu practice -- a SYN to a port with
        no listener is dropped rather than refused, so `connect` ends in a timeout. That is
        precisely the signature of a **free** port, and the code raised
        `host_port_route_probe_failed` and abandoned the installation on it. Measured on the
        owner's server: port 22, which is busy, answers; every free port the installer wanted
        times out. Three of the four ingress modes reach this check, and only
        `external_traefik` escaped it, by publishing nothing.

        So a timeout now means "no listener observed" and the bind probe decides. Nothing is
        weakened: a port that really is occupied still fails the bind, and an unknown bind
        failure still fails closed there.

        A name that does not resolve is still fatal, and deliberately: `_open_public_probe`
        dials this same route after the stack is up to verify the published URL, and if the
        name resolves nowhere that verification cannot work either. A filtered route is
        different -- the same measurement shows it carries traffic as soon as something
        listens -- so it must not be treated the same way.
        """
        connection: socket.socket | None = None
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.settimeout(1.0)
            connection.connect((_BOOTSTRAP_HOST_ROUTE, port))
        except ConnectionRefusedError:
            return False
        except socket.gaierror as exc:
            cause, remediation = HostPreflightOperations._host_route_unresolved(
                f"{_BOOTSTRAP_HOST_ROUTE}:{port}", exc
            )
            if log is not None:
                log.write(cause)
            raise InstallerFailure(
                code="host_port_route_probe_failed",
                cause=cause,
                remediation=remediation,
                exit_code=7,
                retryable=True,
                step="preflight",
            ) from exc
        except OSError as exc:
            if log is not None:
                log.write(
                    f"Docker host route {_BOOTSTRAP_HOST_ROUTE}:{port} did not answer "
                    f"({type(exc).__name__}: {exc}). A host that drops packets to unlistened "
                    "ports, such as ufw with a DROP input policy, produces this for a free "
                    "port. Treating the port as unclaimed; the bind probe decides."
                )
            return False
        else:
            return True
        finally:
            if connection is not None:
                connection.close()

    def _bootstrap_probe_image_id(self: DockerInstallerGateway) -> str:
        container_id = socket.gethostname().strip()
        inspect_command = [
            "docker",
            "container",
            "inspect",
            container_id,
            "--format",
            "{{.Image}}",
        ]
        try:
            inspected = self.process.run(
                inspect_command,
                cwd=self.repo_root,
                timeout_seconds=10,
                separate_stderr=True,
            )
        except OSError as exc:
            raise InstallerFailure(
                code="host_port_bind_probe_failed",
                cause=f"The Docker host bind probe could not inspect its bootstrap image: {exc}",
                remediation=_MESSAGE_VERIFY_LOCAL_DOCKER_ACCESS_AND_RERUN_INSTALL_SH,
                exit_code=7,
                retryable=True,
                step="preflight",
            ) from exc
        image_id = inspected.stdout.strip()
        if inspected.returncode != 0 or not _IMAGE_ID.fullmatch(image_id):
            cause = inspected.stderr.strip() or inspected.stdout.strip() or "invalid image identity"
            raise InstallerFailure(
                code="host_port_bind_probe_failed",
                cause=f"The Docker host bind probe could not resolve its bootstrap image: {cause}",
                remediation=_MESSAGE_VERIFY_LOCAL_DOCKER_ACCESS_AND_RERUN_INSTALL_SH,
                exit_code=7,
                retryable=True,
                step="preflight",
            )

        return image_id
