"""Health technical operations for the installer adapter."""

from __future__ import annotations

import base64
import http.client
import json
import math
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING
from urllib import parse as urlparse
from urllib import request as urlrequest

from core.application.installer.contracts import (
    BootstrapVerification,
    InstallConfig,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.ports import InstallerLog
from core.application.lifecycle.contracts import (
    DeploymentConfig,
)
from core.application.readiness import (
    CoreReadinessService,
    ReadinessArea,
    ReadinessStatus,
)
from core.infrastructure.installer.gateway_support import (
    _BOOTSTRAP_DOCKER_MODE,
    _BOOTSTRAP_HOST_ROUTE,
    _CLI_READINESS_SCRIPT,
    _COMPACT_CLI_READINESS_SCRIPT,
    _MAX_CLI_READINESS_OUTPUT_BYTES,
    _PUBLIC_ROUTE_READINESS_ATTEMPTS,
    _READINESS_ATTEMPTS,
    _READINESS_DELAY_SECONDS,
    _RUNTIME_CERTIFICATE_PATH,
    _TRANSIENT_CANDIDATE_READINESS_IDS,
    _certificate_fingerprint_of_file,
    _cli_readiness_failure_payload,
    _compose_ps_rows,
    _future_timestamp,
    _read_env,
    _routed_urlopen,
    _valid_cli_readiness_payload,
)
from core.infrastructure.installer.public_probe import (
    BufferedPublicResponse,
    public_probe_body_limit,
)
from core.infrastructure.lifecycle import docker_runtime_support

if TYPE_CHECKING:
    from core.infrastructure.installer.gateway import DockerInstallerGateway


class HealthOperations:
    def reconcile_readiness_capabilities(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        check_ids: tuple[str, ...],
        log: InstallerLog,
    ) -> None:
        scripts = {
            "security_posture": docker_runtime_support.security_posture_reconciliation_script,
            "workflow_knowledge_sync": docker_runtime_support.workflow_knowledge_reconciliation_script,
        }
        if not check_ids or any(check_id not in scripts for check_id in check_ids):
            raise InstallerFailure(
                code="unsupported_readiness_reconciliation",
                cause="Only known derived readiness capabilities can be reconciled.",
                remediation="Resolve unrelated readiness blockers with ./flow-steward doctor.",
                exit_code=4,
                retryable=False,
                step="readiness",
            )
        runtime_service = (
            "compact-runtime" if config.deployment_profile == "compact" else "control-plane"
        )
        for check_id in dict.fromkeys(check_ids):
            self._checked(
                [
                    *self._compose_prefix(config),
                    "exec",
                    "-T",
                    runtime_service,
                    "python",
                    "-c",
                    scripts[check_id](),
                ],
                log=log,
                code="readiness_reconciliation_failed",
                remediation="Inspect the derived capability failure and resume ./install.sh.",
                exit_code=4,
                step="readiness",
                timeout_seconds=300.0,
            )

    def verify_health(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        outcome = self._checked(
            [*self._compose_prefix(config), "ps", "--format", "json"],
            log=log,
            code="container_unhealthy",
            remediation="Inspect unhealthy core containers in the installer log and resume.",
            exit_code=8,
            separate_stderr=True,
        )
        rows = _compose_ps_rows(outcome.stdout)
        expected = self._health_expected_services(config)
        observed = {str(row.get("Service") or row.get("service") or ""): row for row in rows}
        failures: list[str] = []
        for service in sorted(expected):
            row = observed.get(service)
            if row is None:
                failures.append(f"{service}: missing")
                continue
            state = str(row.get("State") or row.get("state") or "").lower()
            health = str(row.get("Health") or row.get("health") or "").lower()
            if state != "running" or health in {"unhealthy", "starting"}:
                failures.append(f"{service}: state={state or 'unknown'}, health={health or 'none'}")
        if failures:
            raise InstallerFailure(
                code="container_unhealthy",
                cause="Core services are not ready: " + "; ".join(failures),
                remediation="Inspect the listed containers in the installer log and rerun ./install.sh.",
                exit_code=8,
                step="health",
            )

    def probe_cli_readiness(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Run explicit-config readiness inside the retained exact CLI runtime."""
        remaining_seconds: Callable[[], float] | None = None
        if timeout_seconds is not None:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                return _cli_readiness_failure_payload()
            deadline = monotonic() + timeout_seconds

            def remaining() -> float:
                value = deadline - monotonic()
                if value <= 0:
                    raise TimeoutError("CLI readiness deadline expired")
                return value

            remaining_seconds = remaining
        try:
            if config.deployment_profile == "compact":
                return self._probe_compact_cli_readiness(
                    config, remaining_seconds=remaining_seconds
                )
            context = self._cli_runtime_context(
                config,
                log=None,
                **({"remaining_seconds": remaining_seconds} if remaining_seconds else {}),
            )
            command = [
                *context.compose_prefix,
                "exec",
                "-T",
                "fs-cli",
                "python",
                "-c",
                _CLI_READINESS_SCRIPT,
            ]
            for attempt in range(1, _READINESS_ATTEMPTS + 1):
                outcome = self.process.run(
                    command,
                    cwd=self.repo_root,
                    environment=context.environment,
                    input_text=config.model_dump_json(),
                    separate_stderr=True,
                    **({"timeout_seconds": remaining_seconds()} if remaining_seconds else {}),
                )
                if remaining_seconds:
                    remaining_seconds()
                lines = outcome.stdout.splitlines()
                if (
                    outcome.returncode != 0
                    or outcome.stderr != ""
                    or len(outcome.stdout.encode("utf-8")) > _MAX_CLI_READINESS_OUTPUT_BYTES
                    or len(lines) != 1
                ):
                    raise ValueError("invalid CLI readiness process result")
                payload = json.loads(lines[0])
                if not _valid_cli_readiness_payload(payload):
                    raise ValueError("invalid CLI readiness payload")
                report = CoreReadinessService(probe=lambda payload=payload: payload).check()
                core_failed_ids = {
                    finding.check_id
                    for finding in report.findings
                    if finding.area is ReadinessArea.CORE
                    and finding.status is ReadinessStatus.FAILED
                }
                if (
                    not core_failed_ids
                    or not core_failed_ids.issubset(_TRANSIENT_CANDIDATE_READINESS_IDS)
                    or attempt == _READINESS_ATTEMPTS
                ):
                    return payload
                self._sleep(
                    min(_READINESS_DELAY_SECONDS, remaining_seconds())
                    if remaining_seconds
                    else _READINESS_DELAY_SECONDS
                )
            return _cli_readiness_failure_payload()
        except Exception:
            return _cli_readiness_failure_payload()

    def _probe_compact_cli_readiness(
        self: DockerInstallerGateway,
        config: DeploymentConfig,
        *,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> dict[str, object]:
        command = [
            *self._compose_prefix(config),
            "exec",
            "-T",
            "compact-runtime",
            "python",
            "-c",
            _COMPACT_CLI_READINESS_SCRIPT,
        ]
        for attempt in range(1, _READINESS_ATTEMPTS + 1):
            try:
                outcome = self.process.run(
                    command,
                    cwd=self.repo_root,
                    separate_stderr=True,
                    **({"timeout_seconds": remaining_seconds()} if remaining_seconds else {}),
                )
                if remaining_seconds:
                    remaining_seconds()
                lines = outcome.stdout.splitlines()
                if (
                    outcome.returncode != 0
                    or outcome.stderr != ""
                    or len(outcome.stdout.encode("utf-8")) > _MAX_CLI_READINESS_OUTPUT_BYTES
                    or len(lines) != 1
                ):
                    raise ValueError("invalid Compact CLI readiness process result")
                payload = json.loads(lines[0])
                if not _valid_cli_readiness_payload(payload):
                    raise ValueError("invalid Compact CLI readiness payload")
                report = CoreReadinessService(probe=lambda payload=payload: payload).check()
                if remaining_seconds:
                    core_failed_ids = {
                        finding.check_id
                        for finding in report.findings
                        if finding.area is ReadinessArea.CORE
                        and finding.status is ReadinessStatus.FAILED
                    }
                    if not core_failed_ids.issubset(_TRANSIENT_CANDIDATE_READINESS_IDS):
                        return payload
                if not report.core_failed or attempt == _READINESS_ATTEMPTS:
                    return payload
            except (OSError, TypeError, ValueError):
                if attempt == _READINESS_ATTEMPTS:
                    return _cli_readiness_failure_payload()
            self._sleep(
                min(_READINESS_DELAY_SECONDS, remaining_seconds())
                if remaining_seconds
                else _READINESS_DELAY_SECONDS
            )
        return _cli_readiness_failure_payload()

    def _open_public_probe(
        self: DockerInstallerGateway,
        url: str,
        *,
        config: InstallConfig,
        context: ssl.SSLContext | None,
    ) -> http.client.HTTPResponse | BufferedPublicResponse:
        if self._environment.get("FS_INSTALLER_USE_HOST_PYTHON") != _BOOTSTRAP_DOCKER_MODE:
            return urlrequest.urlopen(  # nosec B310 -- product_url emits only http(s)
                url,
                timeout=10,
                context=context,
            )

        public = urlparse.urlsplit(self.product_url(config))
        server_hostname = public.hostname
        dial_port = config.public_port
        routed_url = url
        routed_context = context
        if config.tls_mode == "reverse_proxy":
            routed_url = urlparse.urlunsplit(("http", public.netloc, *urlparse.urlsplit(url)[2:]))
            server_hostname = None
            dial_port = config.reverse_proxy_backend_port
            routed_context = None

        request = urlrequest.Request(
            routed_url,
            headers={"Host": public.netloc},
        )
        if self._native_linux_public_probe_supported():
            return self._open_host_public_probe(
                request,
                config=config,
                dial_port=dial_port,
                server_hostname=server_hostname,
                context=routed_context,
            )
        return _routed_urlopen(
            request,
            dial_host=_BOOTSTRAP_HOST_ROUTE,
            dial_port=dial_port,
            server_hostname=server_hostname,
            timeout=10,
            context=routed_context,
        )

    def _native_linux_public_probe_supported(self: DockerInstallerGateway) -> bool:
        """Desktop's VM gateway and rootless namespaces are not the native host."""
        try:
            outcome = self.process.run(
                ["docker", "info", "--format", "{{json .}}"],
                cwd=self.repo_root,
                timeout_seconds=10,
                separate_stderr=True,
            )
            if outcome.returncode != 0:
                return False
            info = json.loads(outcome.stdout)
        except (OSError, ValueError):
            return False
        if not isinstance(info, dict):
            return False
        operating_system = str(info.get("OperatingSystem") or "").lower()
        kernel = str(info.get("KernelVersion") or "").lower()
        security = str(info.get("SecurityOptions") or "").lower()
        return (
            info.get("OSType") == "linux"
            and bool(operating_system)
            and "desktop" not in operating_system
            and "linuxkit" not in kernel
            and "rootless" not in security
        )

    def _open_host_public_probe(
        self: DockerInstallerGateway,
        request: urlrequest.Request,
        *,
        config: InstallConfig,
        dial_port: int,
        server_hostname: str | None,
        context: ssl.SSLContext | None,
    ) -> BufferedPublicResponse:
        """Only the read-only probe shares the host network; no socket/secret mounts."""
        dial_host = "::1" if config.public_host.strip("[]") == "::1" else "127.0.0.1"
        if config.tls_mode == "reverse_proxy":
            dial_host = config.reverse_proxy_backend_host.strip("[]")
        ca_certificates = None
        if context is not None:
            if config.tls_mode in {"local", "self_signed", "provided"}:
                ca_certificates = (self.repo_root / _RUNTIME_CERTIFICATE_PATH).read_text(
                    encoding="utf-8"
                )
            else:
                ca_certificates = "".join(
                    ssl.DER_cert_to_PEM_cert(certificate)
                    for certificate in context.get_ca_certs(binary_form=True)
                )
        probe_file = self.repo_root / "core/infrastructure/installer/public_probe.py"
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "host",
            "-i",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--label",
            "com.flow-steward.management=public-probe",
            "--label",
            f"com.flow-steward.installation={self.repo_root}",
            "--mount",
            f"type=bind,src={probe_file},dst=/opt/fs-public-probe.py,readonly",
            "--entrypoint",
            "python3",
            self._bootstrap_probe_image_id(),
            "/opt/fs-public-probe.py",
        ]
        outcome = self.process.run(
            command,
            cwd=self.repo_root,
            input_text=json.dumps(
                {
                    "url": request.full_url,
                    "authority": request.get_header("Host"),
                    "dial_host": dial_host,
                    "dial_port": dial_port,
                    "server_hostname": server_hostname,
                    "ca_certificates": ca_certificates,
                }
            ),
            timeout_seconds=30,
            separate_stderr=True,
        )
        body_limit = public_probe_body_limit(request.full_url)
        if outcome.returncode != 0 or outcome.stderr or len(outcome.stdout) > 2 * body_limit:
            raise OSError("disposable public route probe failed")
        try:
            payload = json.loads(outcome.stdout)
            if not isinstance(payload, dict):
                raise ValueError("invalid probe payload")
            if payload.get("error_kind") == "certificate":
                raise ssl.SSLCertVerificationError(
                    str(payload.get("message") or "certificate verification failed")
                )
            if payload.get("error_kind"):
                raise OSError(str(payload.get("message") or "public route probe failed"))
            status = payload["status"]
            body = base64.b64decode(payload["body"], validate=True)
            if type(status) is not int or not 100 <= status <= 599 or len(body) > body_limit:
                raise ValueError("invalid probe HTTP response")
        except ssl.SSLCertVerificationError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise OSError("invalid disposable public route probe result") from exc
        return BufferedPublicResponse(status, body)

    def verify_public_route(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        url = self.product_url(config) + "/health"
        context = self._public_tls_context(config)
        last_error: OSError | None = None
        for attempt in range(1, _PUBLIC_ROUTE_READINESS_ATTEMPTS + 1):
            try:
                for target in (url, self.product_url(config) + "/"):
                    with self._open_public_probe(
                        target,
                        config=config,
                        context=context,
                    ) as response:
                        if int(response.status) >= 400:
                            raise OSError(f"HTTP {response.status}")
            except OSError as exc:
                last_error = exc
                if attempt < _PUBLIC_ROUTE_READINESS_ATTEMPTS:
                    self._readiness_wait(
                        log,
                        "public route",
                        attempt,
                        total_attempts=_PUBLIC_ROUTE_READINESS_ATTEMPTS,
                    )
                    continue
                break
            log.write(f"Verified public health route at {url}.")
            return
        assert last_error is not None
        if isinstance(last_error, ssl.SSLCertVerificationError):
            raise self._public_certificate_failure(last_error) from last_error
        unresolved = getattr(last_error, "reason", last_error)
        if isinstance(unresolved, socket.gaierror):
            cause, remediation = self._host_route_unresolved(_BOOTSTRAP_HOST_ROUTE, unresolved)
            raise InstallerFailure(
                code="public_health_timeout",
                cause=cause,
                remediation=remediation,
                exit_code=8,
                step="public_route",
            ) from last_error
        raise InstallerFailure(
            code="public_health_timeout",
            cause=f"The public health route is not reachable: {last_error}",
            remediation=(
                f"Inspect Traefik and {self._operator_container('fs-app', config)} "
                "health, then resume ./install.sh."
            ),
            exit_code=8,
            step="public_route",
        ) from last_error

    def _public_certificate_failure(
        self: DockerInstallerGateway, error: ssl.SSLCertVerificationError
    ) -> InstallerFailure:
        """Name the certificate instead of blaming components that are healthy.

        AC17-R83: the route answered 200 on every path and both Traefik and fs-app were
        healthy; what failed was that the installation's own certificate was never the one
        served. "Inspect Traefik and fs-app health" sent the operator to look at the two
        things that were working. Which certificate is involved is the whole question here,
        so the failure names this installation's own and how to compare against it.
        """
        expected_path = self.repo_root / _RUNTIME_CERTIFICATE_PATH
        expected = _certificate_fingerprint_of_file(expected_path)
        detail = (
            f" This installation's own certificate is sha256 {expected} (first 16 of the "
            "DER digest)."
            if expected
            else ""
        )
        return InstallerFailure(
            code="public_certificate_mismatch",
            cause=(
                f"The public route did not present this installation's certificate: "
                f"{error}.{detail}"
            ),
            remediation=(
                f"Compare what the endpoint serves against {_RUNTIME_CERTIFICATE_PATH}: "
                "`openssl s_client -connect <host>:<port> </dev/null | openssl x509 -noout "
                "-subject -fingerprint -sha256`. "
                "A default or placeholder certificate here means the ingress matched no "
                "certificate for this name and fell back to its own, so it must publish a "
                "default certificate as well as per-name ones. Correct the ingress TLS "
                "configuration and resume ./install.sh."
            ),
            exit_code=8,
            step="public_route",
        )

    def verify_bootstrap(
        self: DockerInstallerGateway,
        config: InstallConfig,
        *,
        log: InstallerLog,
        require_first_admin: bool = True,
    ) -> BootstrapVerification:
        url = self.product_url(config) + "/api/app/bootstrap"
        last_error: Exception | None = None
        bootstrap_required = False
        verified = False
        for attempt in range(1, _READINESS_ATTEMPTS + 1):
            try:
                bootstrap_required = self._probe_bootstrap_required(
                    config, url, require_first_admin=require_first_admin
                )
            except (OSError, ValueError) as exc:
                last_error = exc
                if attempt < _READINESS_ATTEMPTS:
                    self._readiness_wait(log, "bootstrap state", attempt)
                    continue
                break
            verified = True
            break
        if not verified:
            assert last_error is not None
            raise InstallerFailure(
                code="bootstrap_state_unavailable",
                cause=f"The application did not report bootstrap state: {last_error}",
                remediation=(
                    f"Inspect {self._operator_container('fs-app', config)} health and "
                    "the installer log, then resume ./install.sh."
                ),
                exit_code=8,
                step="bootstrap",
            ) from last_error
        log.write("Verified first-admin bootstrap state without exposing its token.")
        path = self.state_store.root / "secret-output/bootstrap-token"
        token_expired = False
        token_path: str | None = None
        token = ""
        # AC17-R93: this used to skip the handoff on a loopback host, because a
        # loopback request could once bootstrap without a token. It cannot any more:
        # the installer always writes FS_AUTH_BOOTSTRAP_STATE_FILE above, and
        # bootstrap_request_allowed() returns on that state file before it ever
        # reaches the FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY allowance. Withholding the
        # link on loopback therefore left a `localhost` installation with a token
        # that is mandatory, minted, and never handed to anyone.
        if bootstrap_required:
            expiry = _read_env(self._env_path(config)).get("FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT", "")
            token_expired = not _future_timestamp(expiry)
            if path.is_file() and not token_expired:
                token_path = str(
                    path.relative_to(self.repo_root)
                    if path.is_relative_to(self.repo_root)
                    else path.resolve()
                )
                try:
                    token = path.read_text(encoding="utf-8").strip()
                except OSError:
                    token = ""
        return BootstrapVerification(
            bootstrap_required=bootstrap_required,
            token_path=token_path,
            token_expired=token_expired,
            token=token,
        )

    def _probe_bootstrap_required(
        self: DockerInstallerGateway, config: InstallConfig, url: str, *, require_first_admin: bool
    ) -> bool:
        context = self._public_tls_context(config)
        with self._open_public_probe(
            url,
            config=config,
            context=context,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        auth = payload.get("auth") if isinstance(payload, dict) else None
        if not isinstance(auth, dict) or not isinstance(auth.get("bootstrap_required"), bool):
            raise ValueError("response did not report auth.bootstrap_required")
        bootstrap_required = bool(auth["bootstrap_required"])
        if require_first_admin and not bootstrap_required:
            raise ValueError("response did not require first-administrator bootstrap")
        return bootstrap_required

    def _public_tls_context(
        self: DockerInstallerGateway, config: InstallConfig
    ) -> ssl.SSLContext | None:
        if config.tls_mode == "reverse_proxy":
            return None
        if config.tls_mode in {"local", "self_signed", "provided"}:
            return ssl.create_default_context(
                cafile=str(self.repo_root / _RUNTIME_CERTIFICATE_PATH)
            )
        return ssl.create_default_context()

    def _readiness_wait(
        self: DockerInstallerGateway,
        log: InstallerLog,
        target: str,
        attempt: int,
        *,
        total_attempts: int = _READINESS_ATTEMPTS,
    ) -> None:
        message = (
            f"Waiting for {target} readiness "
            f"({attempt}/{total_attempts}); retrying in {_READINESS_DELAY_SECONDS:g}s."
        )
        log.write(message)
        if self._technical_output_sink is not None:
            self._technical_output_sink(message)
        self._sleep(_READINESS_DELAY_SECONDS)

    def capture_initial_manifest(
        self: DockerInstallerGateway, config: InstallConfig, *, log: InstallerLog
    ) -> None:
        path = self.state_store.root / "initial-preservation-manifest.json"
        evidence = self.inspect()
        if not evidence.resource_inventory_valid:
            raise InstallerFailure(
                code="preservation_inventory_unavailable",
                cause="Docker resource inventory could not be read for the preservation manifest.",
                remediation="Restore Docker access and rerun ./install.sh before treating installation as complete.",
                exit_code=8,
                retryable=True,
                step="manifest",
            )
        payload = {
            "schema_version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "compose_project": self._project_identity(),
            "environment": config.environment,
            "file_storage": config.file_storage,
            "owned_containers": list(evidence.owned_containers),
            "owned_volumes": list(evidence.owned_volumes),
            "durable_secrets_present": evidence.durable_secrets_present,
            "backup_encryption_key_present": evidence.backup_encryption_key_present,
        }
        self.state_store.write_owner_only_text(
            path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        log.write(f"Captured initial preservation manifest at {path}.")
