"""The checks `./flow-steward doctor` only names a next step for (the doctor task, D2).

Their repairs are destructive, or lie outside the product -- a migration, a restore, a
backup, a certificate, the public network, the Docker daemon -- so doctor never makes
them itself (principle 3): each finding says what is wrong and the command or runbook
section that fixes it. Every fact is read fresh from the installation through the probe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from core.application.doctor.checks import MaintenanceFacts
from core.application.doctor.contracts import DoctorCheck, DoctorFinding

BACKUP_MAX_AGE = timedelta(days=7)
CERTIFICATE_WARNING = timedelta(days=14)  # the operator renews a provided certificate
# ./install.sh regenerates the bundled certificate once it is within a day of expiring
# (openssl x509 -checkend 86400), so before that there is nothing to do.
BUNDLED_RENEWAL = timedelta(days=1)
_UPGRADE = (
    "./flow-steward backup create, then "
    "./flow-steward database upgrade --maintenance-confirmed --confirm"
)
_RESTORE = "restore a backup of this release (docs/operator-postgresql-restore-runbook.md)"


@dataclass(frozen=True)
class MigrationFacts:
    pending: tuple[str, ...] = ()
    drifted: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()  # applied by a newer release


@dataclass(frozen=True)
class BackupFacts:
    key_present: bool
    newest_verified: str = ""  # its name
    newest_verified_at: datetime | None = None
    unverified: int = 0


@dataclass(frozen=True)
class CertificateFacts:
    path: str
    not_after: datetime | None  # None: the file is missing or unreadable
    provided: bool  # the operator's own certificate, not the bundled one
    input_path: str = ""  # renewal input; the observed installed certificate is path


@dataclass(frozen=True)
class WorkerFacts:
    expected: int
    running: int
    unit: str  # "fs-worker replicas" or "worker processes"
    apply_command: str


class HintProbe(Protocol):
    profile: str

    def migration_state(self) -> MigrationFacts: ...

    def backup_key_present(self) -> bool: ...  # read on its own: it needs no listing

    def backups(self) -> BackupFacts: ...

    # None: the ingress owns the certificate (ACME, a platform), not a file here
    def certificate(self) -> CertificateFacts | None: ...

    def public_route_problem(self) -> tuple[str, str]: ...  # ("", "") when it answers

    def host_gateway_problem(self) -> tuple[str, str]: ...  # (cause, remediation)

    def queue_consumers(self) -> int | None: ...  # None: the queue does not exist yet

    def maintenance(self) -> MaintenanceFacts | None: ...

    def worker_counts(self) -> WorkerFacts: ...

    def missing_images(self) -> list[tuple[str, str]]: ...  # (variable, value)

    def now(self) -> datetime: ...


def _ok(check_id: str, cause: str) -> DoctorFinding:
    return DoctorFinding(check_id, "ok", cause=cause)


def _hint(check_id: str, status: str, cause: str, next_step: str) -> DoctorFinding:
    return DoctorFinding(check_id, status, cause=cause, next_step=next_step)  # type: ignore[arg-type]


def _names(values: tuple[str, ...]) -> str:
    return ", ".join(values[:5]) + (f" and {len(values) - 5} more" if len(values) > 5 else "")


def _schema_state(probe: HintProbe) -> DoctorFinding:
    facts = probe.migration_state()
    if facts.unknown:
        return _hint(
            "schema_state",
            "fail",
            f"The database has migrations this release does not know ({_names(facts.unknown)}):"
            " a newer release upgraded it, and this one cannot run on it.",
            "./install.sh from the sources of that newer release, or " + _RESTORE,
        )
    if facts.drifted:
        return _hint(
            "schema_state",
            "fail",
            f"Applied migrations no longer match their files: {_names(facts.drifted)}.",
            _RESTORE + "; a migration that has run is never edited",
        )
    if facts.pending:
        return _hint(
            "schema_state",
            "fail",
            f"Migrations this release needs are not applied: {_names(facts.pending)}.",
            _UPGRADE,
        )
    return _ok("schema_state", "The database schema matches this release.")


def _backup_freshness(probe: HintProbe) -> DoctorFinding:
    if not probe.backup_key_present():
        return _hint(
            "backup_freshness",
            "fail",
            "FS_BACKUP_ENCRYPTION_KEY is not in .env.prod: no backup can be made, and the "
            "ones there cannot be read.",
            "Put back the FS_BACKUP_ENCRYPTION_KEY this installation used (from where you "
            "keep it) into .env.prod; a new key cannot read the old backups.",
        )
    facts = probe.backups()
    if facts.newest_verified_at is None:
        unverified = (
            f" ({facts.unverified} not verified: ./flow-steward backup verify <name>)"
            if facts.unverified
            else ""
        )
        return _hint(
            "backup_freshness",
            "warn",
            "There is no verified backup" + unverified + ".",
            "./flow-steward backup create",
        )
    age = probe.now() - facts.newest_verified_at
    if age > BACKUP_MAX_AGE:
        return _hint(
            "backup_freshness",
            "warn",
            f"The newest verified backup, {facts.newest_verified}, is {age.days} days old.",
            "./flow-steward backup create",
        )
    return _ok(
        "backup_freshness",
        f"The newest verified backup, {facts.newest_verified}, is {age.days} days old.",
    )


def _certificate_expiry(probe: HintProbe) -> DoctorFinding:
    facts = probe.certificate()
    if facts is None:
        return _ok("certificate_expiry", "The ingress manages the certificate, not a file here.")
    renew = (
        f"Replace {facts.input_path or facts.path} with a renewed certificate, then "
        "./flow-steward config apply --confirm"
        if facts.provided
        else "./install.sh (it renews the bundled certificate within a day of expiry)"
    )
    if facts.not_after is None:
        return _hint(
            "certificate_expiry",
            "fail",
            f"The certificate {facts.path} is missing or cannot be read.",
            renew,
        )
    left = facts.not_after - probe.now()
    expires = facts.not_after.astimezone(UTC).strftime("%Y-%m-%d")
    if left <= timedelta(0):
        return _hint(
            "certificate_expiry",
            "fail",
            f"The certificate {facts.path} expired on {expires}.",
            renew,
        )
    if not facts.provided and left <= BUNDLED_RENEWAL:
        return _hint(
            "certificate_expiry",
            "fail",
            f"The bundled certificate {facts.path} expires on {expires}.",
            renew,
        )
    if facts.provided and left <= CERTIFICATE_WARNING:
        return _hint(
            "certificate_expiry",
            "warn",
            f"The certificate {facts.path} expires on {expires}, in {left.days} days.",
            renew,
        )
    return _ok("certificate_expiry", f"The certificate is valid until {expires}.")


def _public_route(probe: HintProbe) -> DoctorFinding:
    cause, next_step = probe.public_route_problem()
    if not cause:
        return _ok("public_route", "The public URL answers.")
    return _hint("public_route", "fail", cause, next_step)


def _docker_host_gateway(probe: HintProbe) -> DoctorFinding:
    cause, next_step = probe.host_gateway_problem()
    if not cause:
        return _ok("docker_host_gateway", "Docker resolves host-gateway to an address.")
    return _hint("docker_host_gateway", "fail", cause, next_step)


def _workers_consuming(probe: HintProbe) -> DoctorFinding:
    consumers = probe.queue_consumers()
    if consumers is None:
        return DoctorFinding(
            "workers_consuming",
            "info",
            cause="The job queue does not exist yet: no worker has connected since the install.",
            next_step="./flow-steward status",
        )
    if consumers < 1:
        maintenance = probe.maintenance()
        if maintenance is not None and maintenance.phase == "enabled":
            return DoctorFinding(
                "workers_consuming",
                "info",
                cause="Workers intentionally pause job queue consumption while maintenance is enabled.",
                next_step="./flow-steward maintenance status",
            )
        return _hint(
            "workers_consuming",
            "fail",
            "No worker consumes the job queue: submitted jobs wait for ever.",
            "./flow-steward doctor --fix stack_stopped if a worker is stopped; otherwise "
            "./flow-steward stack restart",
        )
    return _ok("workers_consuming", f"{consumers} consumers on the job queue.")


def _worker_count_recorded(probe: HintProbe) -> DoctorFinding:
    facts = probe.worker_counts()
    if facts.running == facts.expected:
        return _ok("worker_count_recorded", f"{facts.running} {facts.unit}, as configured.")
    return _hint(
        "worker_count_recorded",
        "fail",
        f"{facts.running} {facts.unit} run, and the configuration says {facts.expected}.",
        facts.apply_command,
    )


def _recorded_images_present(probe: HintProbe) -> DoctorFinding:
    missing = probe.missing_images()
    if not missing:
        return _ok("recorded_images_present", "Every image .env.prod names is on this host.")
    words = "; ".join(f"{name}={value}" for name, value in missing)
    unpullable = any(value.startswith("sha256:") for _name, value in missing)
    return _hint(
        "recorded_images_present",
        "fail",
        f"Not on this host: {words}."
        + (" An image id cannot be pulled back from any registry." if unpullable else "")
        + " Neither a start nor an update can create these containers without them.",
        # AC17-R177b: ./install.sh builds again an image it built itself, and refuses by
        # name one it did not -- which only its owner can bring back.
        "./install.sh builds again the images it built itself. For one you chose: restore "
        "it (docker load from a saved copy, or docker pull for a registry reference) or "
        "clear the value in .env.prod, then rerun ./install.sh.",
    )


def hint_checks() -> tuple[DoctorCheck, ...]:
    return (
        DoctorCheck("schema_state", "Database schema", _schema_state),
        DoctorCheck("backup_freshness", "Verified backup", _backup_freshness),
        DoctorCheck("certificate_expiry", "TLS certificate", _certificate_expiry),
        DoctorCheck("public_route", "Public URL", _public_route),
        DoctorCheck("docker_host_gateway", "Docker host route", _docker_host_gateway),
        DoctorCheck("workers_consuming", "Workers on the job queue", _workers_consuming),
        DoctorCheck("worker_count_recorded", "Worker count", _worker_count_recorded),
        DoctorCheck("recorded_images_present", "Recorded images", _recorded_images_present),
    )
