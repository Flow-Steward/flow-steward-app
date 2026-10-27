"""The checks doctor only names a next step for (the doctor task, D2): each from the
broken side -- with the command or runbook that fixes it, and never a repair -- and from
the healthy side."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from core.application.doctor import hints
from core.application.doctor.checks import MaintenanceFacts
from core.application.doctor.contracts import DoctorFinding
from core.application.doctor.hints import (
    BackupFacts,
    CertificateFacts,
    MigrationFacts,
    WorkerFacts,
)

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@dataclass
class _Probe:
    profile: str = "compact"
    migrations: MigrationFacts = field(default_factory=MigrationFacts)
    backup: BackupFacts = field(
        default_factory=lambda: BackupFacts(
            key_present=True, newest_verified="b.dump.enc", newest_verified_at=_NOW
        )
    )
    cert: CertificateFacts | None = None
    route: tuple[str, str] = ("", "")
    host_route: tuple[str, str] = ("", "")
    consumers: int | None = 1
    maintenance_facts: MaintenanceFacts | None = None
    workers: WorkerFacts = field(
        default_factory=lambda: WorkerFacts(2, 2, "worker processes", "./install.sh")
    )
    images: list[tuple[str, str]] = field(default_factory=list)
    listing_fails: bool = False

    def migration_state(self) -> MigrationFacts:
        return self.migrations

    def backup_key_present(self) -> bool:
        return self.backup.key_present

    def backups(self) -> BackupFacts:
        if self.listing_fails:
            raise OSError("the backups could not be listed")
        return self.backup

    def certificate(self) -> CertificateFacts | None:
        return self.cert

    def public_route_problem(self) -> tuple[str, str]:
        return self.route

    def host_gateway_problem(self) -> tuple[str, str]:
        return self.host_route

    def queue_consumers(self) -> int | None:
        return self.consumers

    def maintenance(self) -> MaintenanceFacts | None:
        return self.maintenance_facts

    def worker_counts(self) -> WorkerFacts:
        return self.workers

    def missing_images(self) -> list[tuple[str, str]]:
        return self.images

    def now(self) -> datetime:
        return _NOW


def _diagnose(check_id: str, probe: _Probe) -> DoctorFinding:
    (check,) = [check for check in hints.hint_checks() if check.check_id == check_id]
    assert check.repair is None  # principle 3: a hint, never a repair
    return check.diagnose(probe)


def test_the_hint_set() -> None:
    assert [check.check_id for check in hints.hint_checks()] == [
        "schema_state",
        "backup_freshness",
        "certificate_expiry",
        "public_route",
        "docker_host_gateway",
        "workers_consuming",
        "worker_count_recorded",
        "recorded_images_present",
    ]


def test_the_healthy_installation_passes_every_hint() -> None:
    probe = _Probe(cert=CertificateFacts(".ssl/cert.pem", _NOW + timedelta(days=300), False))

    assert {c.check_id: c.diagnose(probe).status for c in hints.hint_checks()} == {
        check.check_id: "ok" for check in hints.hint_checks()
    }


# -- schema_state -----------------------------------------------------------------------------


def test_pending_migrations_name_the_backup_then_the_upgrade() -> None:
    finding = _diagnose("schema_state", _Probe(migrations=MigrationFacts(pending=("0003.sql",))))

    assert finding.status == "fail"
    assert "0003.sql" in finding.cause
    assert finding.next_step == (
        "./flow-steward backup create, then "
        "./flow-steward database upgrade --maintenance-confirmed --confirm"
    )


def test_a_database_a_newer_release_upgraded_is_named_before_anything_else() -> None:
    facts = MigrationFacts(pending=("0003.sql",), unknown=("0009_newer.sql",))

    finding = _diagnose("schema_state", _Probe(migrations=facts))

    assert "0009_newer.sql" in finding.cause
    assert "0003.sql" not in finding.cause  # the upgrade would be the wrong advice here
    assert "operator-postgresql-restore-runbook.md" in finding.next_step


def test_drifted_migrations_point_to_the_restore_runbook() -> None:
    finding = _diagnose("schema_state", _Probe(migrations=MigrationFacts(drifted=("0002.sql",))))

    assert finding.status == "fail"
    assert "docs/operator-postgresql-restore-runbook.md" in finding.next_step


# -- backup_freshness -------------------------------------------------------------------------


def test_a_missing_backup_key_is_a_failure_and_a_new_key_is_not_the_advice() -> None:
    finding = _diagnose("backup_freshness", _Probe(backup=BackupFacts(key_present=False)))

    assert finding.status == "fail"
    assert "a new key cannot read the old backups" in finding.next_step


def test_a_missing_backup_key_is_named_even_when_the_listing_cannot_run() -> None:
    """F3 (live on fs-acc): without the key every check crashed, this one included."""
    probe = _Probe(backup=BackupFacts(key_present=False), listing_fails=True)

    finding = _diagnose("backup_freshness", probe)

    assert finding.status == "fail"
    assert "FS_BACKUP_ENCRYPTION_KEY is not in .env.prod" in finding.cause


def test_no_verified_backup_is_a_warning_naming_the_unverified_ones() -> None:
    finding = _diagnose(
        "backup_freshness", _Probe(backup=BackupFacts(key_present=True, unverified=2))
    )

    assert finding.status == "warn"
    assert "2 not verified" in finding.cause
    assert finding.next_step == "./flow-steward backup create"


def test_an_old_verified_backup_is_a_warning_and_a_recent_one_passes() -> None:
    old = BackupFacts(
        key_present=True,
        newest_verified="old.dump.enc",
        newest_verified_at=_NOW - timedelta(days=8),
    )
    recent = BackupFacts(
        key_present=True,
        newest_verified="new.dump.enc",
        newest_verified_at=_NOW - timedelta(days=7),
    )

    assert _diagnose("backup_freshness", _Probe(backup=old)).status == "warn"
    assert "8 days old" in _diagnose("backup_freshness", _Probe(backup=old)).cause
    assert _diagnose("backup_freshness", _Probe(backup=recent)).status == "ok"


# -- certificate_expiry -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days_left", "provided", "status"),
    [
        (-1, True, "fail"),
        (10, True, "warn"),  # the operator renews a certificate they provided
        (20, True, "ok"),
        (-1, False, "fail"),
        (0.5, False, "fail"),  # ./install.sh renews the bundled one within a day of expiry
        (10, False, "ok"),  # ... and not before: nothing to do yet
    ],
)
def test_certificate_expiry(days_left: float, provided: bool, status: str) -> None:
    facts = CertificateFacts("/srv/fs/.ssl/cert.pem", _NOW + timedelta(days=days_left), provided)

    finding = _diagnose("certificate_expiry", _Probe(cert=facts))

    assert finding.status == status
    if status != "ok":
        expected = "config apply --confirm" if provided else "./install.sh"
        assert expected in finding.next_step


def test_a_missing_certificate_file_is_a_failure() -> None:
    facts = CertificateFacts("/srv/fs/.ssl/cert.pem", None, provided=True)

    finding = _diagnose("certificate_expiry", _Probe(cert=facts))

    assert finding.status == "fail"
    assert "missing or cannot be read" in finding.cause


def test_a_certificate_the_ingress_owns_is_not_a_file_to_check() -> None:
    assert _diagnose("certificate_expiry", _Probe(cert=None)).status == "ok"


# -- public_route, docker_host_gateway --------------------------------------------------------


def test_the_public_route_and_the_host_route_carry_their_probes_words() -> None:
    route = _diagnose(
        "public_route", _Probe(route=("The public URL x does not answer.", "Check DNS"))
    )
    host = _diagnose("docker_host_gateway", _Probe(host_route=("invalid IP", "restart dockerd")))

    assert (route.status, route.cause, route.next_step) == (
        "fail",
        "The public URL x does not answer.",
        "Check DNS",
    )
    assert (host.status, host.next_step) == ("fail", "restart dockerd")


# -- workers ----------------------------------------------------------------------------------


def test_no_consumer_on_the_job_queue_is_a_failure_and_no_queue_yet_is_information() -> None:
    none = _diagnose("workers_consuming", _Probe(consumers=0))
    absent = _diagnose("workers_consuming", _Probe(consumers=None))

    assert none.status == "fail"
    assert "stack restart" in none.next_step
    assert absent.status == "info"


@pytest.mark.parametrize("profile", ["compact", "scalable"])
@pytest.mark.parametrize("owner_kind", ["manual", "release"])
def test_maintenance_paused_consumers_do_not_advise_restarting_workers(
    profile: str, owner_kind: str
) -> None:
    finding = _diagnose(
        "workers_consuming",
        _Probe(
            profile=profile,
            consumers=0,
            maintenance_facts=MaintenanceFacts("enabled", owner_kind, "operator", "now"),
        ),
    )

    assert finding.status == "info"
    assert "maintenance" in finding.cause.lower()
    assert finding.next_step == "./flow-steward maintenance status"
    assert not finding.repairable


@pytest.mark.parametrize("phase", ["disabled", "", "unknown"])
def test_only_enabled_maintenance_explains_zero_consumers(phase: str) -> None:
    finding = _diagnose(
        "workers_consuming",
        _Probe(
            consumers=0,
            maintenance_facts=MaintenanceFacts(phase, "manual", "operator", "now"),
        ),
    )

    assert finding.status == "fail"
    assert "stack restart" in finding.next_step


@pytest.mark.parametrize("consumers", [None, 2])
def test_maintenance_does_not_replace_absent_queue_or_observed_consumers(
    consumers: int | None,
) -> None:
    finding = _diagnose(
        "workers_consuming",
        _Probe(
            consumers=consumers,
            maintenance_facts=MaintenanceFacts("enabled", "manual", "operator", "now"),
        ),
    )

    if consumers is None:
        assert finding.status == "info"
        assert finding.next_step == "./flow-steward status"
    else:
        assert finding.status == "ok"
        assert finding.cause == "2 consumers on the job queue."


def test_unreadable_maintenance_is_not_treated_as_an_expected_pause() -> None:
    class UnreadableProbe(_Probe):
        def maintenance(self) -> MaintenanceFacts | None:
            raise OSError("maintenance state is unreadable")

    probe = UnreadableProbe(consumers=0)
    with pytest.raises(OSError, match="maintenance state is unreadable"):
        _diagnose("workers_consuming", probe)


def test_maintenance_does_not_hide_a_missing_worker_process() -> None:
    finding = _diagnose(
        "worker_count_recorded",
        _Probe(
            consumers=0,
            maintenance_facts=MaintenanceFacts("enabled", "manual", "operator", "now"),
            workers=WorkerFacts(1, 0, "worker processes", "./install.sh"),
        ),
    )

    assert finding.status == "fail"
    assert finding.next_step == "./install.sh"


def test_a_worker_count_off_the_configuration_names_both_and_the_apply() -> None:
    facts = WorkerFacts(3, 1, "fs-worker replicas", "./flow-steward config apply --confirm")

    finding = _diagnose("worker_count_recorded", _Probe(workers=facts))

    assert finding.status == "fail"
    assert finding.cause == "1 fs-worker replicas run, and the configuration says 3."
    assert finding.next_step == "./flow-steward config apply --confirm"


# -- recorded_images_present (AC17-R177) ------------------------------------------------------


def test_a_pruned_recorded_image_is_named_with_its_variable() -> None:
    """Live on fs-ac19-bench (2026-09-25): POSTGRES_IMAGE named a pruned image id, and the
    update failed with "pull access denied for sha256"."""
    missing = [("POSTGRES_IMAGE", "sha256:" + "9" * 64)]

    finding = _diagnose("recorded_images_present", _Probe(images=missing))

    assert finding.status == "fail"
    assert f"POSTGRES_IMAGE=sha256:{'9' * 64}" in finding.cause
    assert "cannot be pulled back" in finding.cause
    assert "docker load" in finding.next_step
    # AC17-R177b: ./install.sh rebuilds its own; only a chosen image is the operator's.
    assert finding.next_step.startswith("./install.sh builds again the images it built itself")
    assert "clear the value in .env.prod" in finding.next_step


def test_a_missing_registry_reference_is_not_called_unpullable() -> None:
    missing = [("FS_COMPACT_IMAGE", "ghcr.io/example/flow-steward-compact:1.0")]

    finding = _diagnose("recorded_images_present", _Probe(images=missing))

    assert finding.status == "fail"
    assert "cannot be pulled back" not in finding.cause


@pytest.mark.parametrize("days_left", [-1, 10])
def test_provided_runtime_certificate_diagnosis_retains_operator_renewal_input(
    days_left: int,
) -> None:
    facts = CertificateFacts(
        "/srv/fs/.ssl/cert.pem",
        _NOW + timedelta(days=days_left),
        provided=True,
        input_path="/operator/provided.pem",
    )
    finding = _diagnose("certificate_expiry", _Probe(cert=facts))
    assert finding.status == ("fail" if days_left < 0 else "warn")
    assert "/srv/fs/.ssl/cert.pem" in finding.cause
    assert "Replace /operator/provided.pem" in finding.next_step
    assert "config apply --confirm" in finding.next_step


def test_generated_runtime_certificate_keeps_installer_renewal_guidance() -> None:
    facts = CertificateFacts(
        "/srv/fs/.ssl/cert.pem",
        _NOW - timedelta(days=1),
        provided=False,
        input_path="/operator/generated.pem",
    )
    finding = _diagnose("certificate_expiry", _Probe(cert=facts))
    assert finding.status == "fail"
    assert "./install.sh" in finding.next_step
    assert "Replace" not in finding.next_step
