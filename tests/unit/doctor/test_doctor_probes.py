"""The Docker-side facts `./flow-steward doctor` reads: each looks without touching (a
lock is found in /proc/locks, never taken; a mode is read with lstat, never through a
link), and a repair acts on the entry itself, never on what a link points at."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.application.installer import InstallConfig
from core.infrastructure.installer import gateway_doctor as doctor_gateway_module
from core.infrastructure.installer.doctor_probes import flock_held, narrow, wide_entries
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.runtime import readiness_runtime
from core.infrastructure.runtime.compact import shared_trees
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# -- flock_held ---------------------------------------------------------------------------


def _lock_line(path: Path, *, waiter: bool = False) -> str:
    info = path.lstat()
    identity = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    return f"1: {'-> ' if waiter else ''}FLOCK  ADVISORY  WRITE 4242 {identity} 0 EOF"


def test_a_held_flock_is_found_by_the_files_identity(tmp_path: Path) -> None:
    lock = tmp_path / "active.lock"
    lock.write_text("", encoding="utf-8")
    locks = tmp_path / "locks"
    locks.write_text("7: POSIX  ADVISORY  READ 1 00:10:99 0 EOF\n" + _lock_line(lock) + "\n")

    assert flock_held(lock, proc_locks=locks) is True


def test_a_waiter_or_another_file_is_not_a_holder(tmp_path: Path) -> None:
    lock, other = tmp_path / "active.lock", tmp_path / "other.lock"
    for path in (lock, other):
        path.write_text("", encoding="utf-8")
    locks = tmp_path / "locks"
    locks.write_text(_lock_line(lock, waiter=True) + "\n" + _lock_line(other) + "\n")

    assert flock_held(lock, proc_locks=locks) is False


def test_a_missing_lock_is_not_held_and_an_unreadable_table_is_unknown(tmp_path: Path) -> None:
    assert flock_held(tmp_path / "absent.lock", proc_locks=tmp_path / "locks") is False
    present = tmp_path / "active.lock"
    present.write_text("", encoding="utf-8")
    assert flock_held(present, proc_locks=tmp_path / "no-such-table") is None


# -- secrets_modes ------------------------------------------------------------------------


def test_wide_entries_are_found_and_links_are_neither_followed_nor_reported(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    (state / "release").mkdir()
    secret = state / "install-journal.json"
    secret.write_text("{}", encoding="utf-8")
    secret.chmod(0o644)
    (state / "logs").chmod(0o755)
    (state / "release" / "archive.tar").write_text("x", encoding="utf-8")
    (state / "release" / "archive.tar").chmod(0o644)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    outside.chmod(0o644)
    (state / "link").symlink_to(outside)
    state.chmod(0o700)

    found = {entry.path: entry for entry in wide_entries(state, exempt=frozenset({"release"}))}

    assert set(found) == {secret, state / "logs"}
    assert found[secret].words().endswith("is 0644, wanted 0600")
    assert found[state / "logs"].is_dir
    assert found[secret].owned


def test_narrow_takes_group_and_other_bits_and_refuses_a_link_put_in_place(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "state.json"
    secret.write_text("{}", encoding="utf-8")
    secret.chmod(0o664)
    (entry,) = wide_entries(secret)

    narrow(entry)

    assert _mode(secret) == 0o600
    # The entry looked at is replaced by a link to something else before the repair.
    target = tmp_path / "elsewhere.txt"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o644)
    secret.chmod(0o644)
    (entry,) = wide_entries(secret)
    secret.unlink()
    secret.symlink_to(target)
    with pytest.raises(OSError):
        narrow(entry)
    assert _mode(target) == 0o644


# -- shared_dirs_modes --------------------------------------------------------------------


def _gateway(root: Path) -> DockerInstallerGateway:
    (root / "infra/docker").mkdir(parents=True, exist_ok=True)
    return DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward", lifecycle_state_root=root),
        environment={},
    )


def _config(**fields: str) -> InstallConfig:
    return InstallConfig(environment="production", **fields)


def test_doctor_can_read_the_installers_disk_preflight_threshold(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    assert gateway.minimum_free_disk_bytes() == 4 * 1024**3


def test_the_volume_trees_are_the_shared_trees_of_a_compact_volume(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    local = gateway.shared_tree_roots(
        _config(
            deployment_profile="compact", data_placement="compact_volume", file_storage="local_fs"
        )
    )
    bundled = gateway.shared_tree_roots(
        _config(
            deployment_profile="compact", data_placement="compact_volume", file_storage="bundled"
        )
    )

    assert local == (
        "volume",
        {
            "extensions": "/data/extensions",
            "artifacts": "/data/artifacts",
        },
    )
    assert bundled is not None
    assert "artifacts" not in bundled[1]  # object storage holds them, not a tree


def test_host_directories_of_compact_use_the_canonical_shared_trees(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    placement = gateway.shared_tree_roots(
        _config(
            deployment_profile="compact", data_placement="host_directories", file_storage="local_fs"
        )
    )

    assert placement == (
        "host",
        {
            "artifacts": str((tmp_path / "logs").resolve() / "artifacts"),
            "extensions": str((tmp_path / "extensions").resolve()),
        },
    )


def test_scalable_on_its_own_directories_has_no_shared_model(tmp_path: Path) -> None:
    placement = _gateway(tmp_path).shared_tree_roots(
        _config(deployment_profile="scalable", file_storage="local_fs")
    )

    assert placement is None


def test_the_doctor_asks_about_graphs_what_readiness_asks() -> None:
    assert doctor_gateway_module._STALE_GRAPHS_QUERY == readiness_runtime._STALE_GRAPHS_SQL


_LAYOUT = CompactVolumeLayout.for_current_user()


def test_inspect_counts_what_is_outside_the_model_and_changes_nothing(tmp_path: Path) -> None:
    model = shared_trees.tree_models(_LAYOUT)["extensions"]
    root = tmp_path / "extensions"
    (root / "run").mkdir(parents=True)
    (root / "run" / "file.txt").write_text("x", encoding="utf-8")
    (root / "run").chmod(0o755)  # no group write, no setgid: off the model
    (root / "run" / "file.txt").chmod(0o644)
    (root / "private.key").write_text("x", encoding="utf-8")
    (root / "private.key").chmod(0o600)  # private: left alone, not counted
    before = {path: _mode(path) for path in (root, root / "run", root / "run" / "file.txt")}

    report = shared_trees.inspect_tree(root, "extensions", model)

    assert report.pending == 3  # the root, the directory and the file
    assert {path: _mode(path) for path in before} == before


def test_a_full_walk_repairs_what_was_added_after_the_tree_was_marked(tmp_path: Path) -> None:
    model = shared_trees.tree_models(_LAYOUT)["extensions"]
    root = tmp_path / "extensions"
    root.mkdir()
    shared_trees.normalize_tree(root, "extensions", model)  # marks the root: walk finished
    late = root / "added-later.txt"
    late.write_text("x", encoding="utf-8")
    late.chmod(0o644)

    marked = shared_trees.normalize_tree(root, "extensions", model)
    assert marked.already  # the green side of an ordinary run: it trusts the mark
    assert _mode(late) == 0o644

    shared_trees.normalize_tree(root, "extensions", model, full=True)

    assert _mode(late) == model.file_mode
    assert shared_trees.inspect_tree(root, "extensions", model).pending == 0


# -- stack_stopped: which sources built the installation ------------------------------------


class _Process:
    """Docker and git as the gateway asks them; answers by the command's start."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def run(self, command, **_kwargs):
        from core.infrastructure.installer.process import ProcessOutcome

        self.commands.append(list(command))
        for prefix, (code, out) in self.answers.items():
            if tuple(command[: len(prefix)]) == prefix:
                return ProcessOutcome(command=tuple(command), returncode=code, stdout=out)
        return ProcessOutcome(command=tuple(command), returncode=1, stdout="")


def _revision_gateway(root: Path, answers) -> DockerInstallerGateway:
    (root / "infra/docker").mkdir(parents=True, exist_ok=True)
    return DockerInstallerGateway(
        repo_root=root,
        state_store=InstallerStateStore(root / ".flow-steward", lifecycle_state_root=root),
        process=_Process(answers),
        environment={"FS_COMPOSE_PROJECT_NAME": "fs-mine"},
    )


_IMAGE = "sha256:" + "e" * 64
_BUILT = "b" * 40


def test_the_installed_revision_is_the_stopped_runtimes_image_label(tmp_path: Path) -> None:
    gateway = _revision_gateway(
        tmp_path,
        {
            ("docker", "ps", "-a"): (0, "c0ffee\n"),
            ("docker", "container", "inspect"): (0, _IMAGE + "\n"),
            ("docker", "image", "inspect"): (0, _BUILT + "\n"),
        },
    )

    revision = gateway.installed_runtime_revision(
        _config(deployment_profile="compact", data_placement="compact_volume")
    )

    assert revision == _BUILT
    listing = gateway.process.commands[0]
    assert "label=com.docker.compose.project=fs-mine" in listing  # this installation's
    assert "label=com.docker.compose.service=compact-runtime" in listing


def test_no_runtime_container_means_the_installed_revision_is_unknown(tmp_path: Path) -> None:
    gateway = _revision_gateway(tmp_path, {("docker", "ps", "-a"): (0, "")})

    assert gateway.installed_runtime_revision(_config(deployment_profile="scalable")) == ""


def test_the_source_revision_is_the_checkouts_head(tmp_path: Path) -> None:
    head = "c" * 40
    assert _revision_gateway(tmp_path, {("git",): (0, head + "\n")}).source_revision() == head
    assert _revision_gateway(tmp_path, {("git",): (128, "")}).source_revision() == ""


# -- the hint checks' reads (D2) ------------------------------------------------------------


def test_the_doctor_asks_rabbitmq_about_the_workers_own_queue_and_states() -> None:
    from core.infrastructure.runtime.compact import readiness as compact_readiness
    from core.infrastructure.runtime.worker import queue_broker as queue

    assert doctor_gateway_module._JOB_QUEUE == queue._READY_QUEUE
    assert (
        frozenset(compact_readiness._READY_WORKER_STATES)
        == doctor_gateway_module._READY_WORKER_STATES
    )


def test_queue_consumers_are_read_from_rabbitmqctl(tmp_path: Path) -> None:
    listing = "platform.dead\t0\nplatform.projects.ready\t2\n"
    gateway = _revision_gateway(tmp_path, {("docker", "compose"): (0, listing)})

    assert gateway.queue_consumers(_config(deployment_profile="compact")) == 2
    (command,) = gateway.process.commands
    assert command[-5:] == ["rabbitmqctl", "-q", "list_queues", "name", "consumers"]


def test_a_queue_that_does_not_exist_yet_has_no_consumer_count(tmp_path: Path) -> None:
    gateway = _revision_gateway(tmp_path, {("docker", "compose"): (0, "")})

    assert gateway.queue_consumers(_config(deployment_profile="compact")) is None


def test_the_certificate_end_date_is_openssls(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    gateway = _revision_gateway(
        tmp_path, {("openssl",): (0, "notAfter=Oct  1 12:00:00 2027 GMT\n")}
    )

    assert gateway.certificate_not_after(tmp_path / "cert.pem") == datetime(
        2027, 10, 1, 12, tzinfo=UTC
    )
    assert _revision_gateway(tmp_path, {}).certificate_not_after(tmp_path / "x.pem") is None


def test_missing_recorded_images_are_named_with_their_variable(tmp_path: Path) -> None:
    pruned = "sha256:" + "9" * 64
    present = "sha256:" + "1" * 64
    (tmp_path / ".env.prod").write_text(
        f"FS_COMPACT_IMAGE={present}\nPOSTGRES_IMAGE={pruned}\nRABBITMQ_IMAGE=rabbitmq:3\n",
        encoding="utf-8",
    )
    gateway = _revision_gateway(tmp_path, {("docker", "image", "inspect", "--format"): (0, "")})
    real_run = gateway.process.run

    def run(command, **kwargs):
        if command[-1] == pruned:
            from core.infrastructure.installer.process import ProcessOutcome

            return ProcessOutcome(command=tuple(command), returncode=1, stdout="")
        return real_run(command, **kwargs)

    gateway.process.run = run

    missing = gateway.missing_recorded_images(_config(deployment_profile="compact"))

    assert missing == [("POSTGRES_IMAGE", pruned)]


def test_the_host_route_is_named_only_when_docker_did_not_map_an_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = tmp_path / "hosts"
    monkeypatch.setattr(doctor_gateway_module, "_HOSTS_FILE", hosts)
    gateway = _gateway(tmp_path)

    hosts.write_text("invalid IP\thost.docker.internal\n", encoding="utf-8")
    cause, remediation = gateway.host_gateway_problem()
    assert "'invalid IP'" in cause
    assert "restart dockerd" in remediation

    hosts.write_text("192.168.65.254\thost.docker.internal\n", encoding="utf-8")
    assert gateway.host_gateway_problem() == ("", "")


@pytest.mark.parametrize("tls_mode", ["provided", "local"])
def test_certificate_probe_observes_owned_runtime_mirror_read_only(
    tmp_path: Path, tls_mode: str
) -> None:
    from datetime import UTC, datetime

    from core.infrastructure.installer.installation_doctor import InstallationDoctorProbe

    mirror = tmp_path / ".ssl/cert.pem"
    mirror.parent.mkdir()
    mirror.write_text("active runtime certificate")
    operator_input = tmp_path / "unmounted-input/certificate.pem"
    (tmp_path / ".env.prod").write_text(f"FS_TLS_CERTIFICATE_PATH={operator_input}\n")
    gateway = _revision_gateway(
        tmp_path, {("openssl",): (0, "notAfter=Oct  1 12:00:00 2027 GMT\n")}
    )
    probe = InstallationDoctorProbe(
        root=tmp_path,
        gateway=gateway,
        state=gateway.state_store,
        config=_config(tls_mode=tls_mode, certificate_path=str(operator_input)),
        log=None,
    )
    before = {p: (p.read_bytes(), _mode(p)) for p in tmp_path.rglob("*") if p.is_file()}
    facts = probe.certificate()
    assert facts is not None
    assert facts.path == str(mirror)
    assert facts.not_after == datetime(2027, 10, 1, 12, tzinfo=UTC)
    assert facts.provided is (tls_mode == "provided")
    assert facts.input_path == str(operator_input)
    assert gateway.process.commands[-1][-1] == str(mirror)
    assert {p: (p.read_bytes(), _mode(p)) for p in tmp_path.rglob("*") if p.is_file()} == before


def test_missing_runtime_mirror_is_not_hidden_by_readable_operator_input(tmp_path: Path) -> None:
    from core.infrastructure.installer.installation_doctor import InstallationDoctorProbe

    operator_input = tmp_path / "operator.pem"
    operator_input.write_text("healthy operator input, not the installed certificate")
    (tmp_path / ".env.prod").write_text(f"FS_TLS_CERTIFICATE_PATH={operator_input}\n")
    gateway = _revision_gateway(
        tmp_path, {("openssl",): (0, "notAfter=Oct  1 12:00:00 2027 GMT\n")}
    )
    probe = InstallationDoctorProbe(
        root=tmp_path,
        gateway=gateway,
        state=gateway.state_store,
        config=_config(tls_mode="provided", certificate_path=str(operator_input)),
        log=None,
    )
    facts = probe.certificate()
    assert facts is not None
    assert facts.path == str(tmp_path / ".ssl/cert.pem")
    assert facts.not_after is None
    assert gateway.process.commands == []


@pytest.mark.parametrize("unreadable", [False, True])
def test_expired_or_unreadable_installed_mirror_is_not_hidden_by_healthy_input(
    tmp_path: Path, unreadable: bool
) -> None:
    from core.application.doctor.hints import _certificate_expiry
    from core.infrastructure.installer.installation_doctor import InstallationDoctorProbe

    mirror = tmp_path / ".ssl/cert.pem"
    mirror.parent.mkdir()
    mirror.write_text("installed certificate")
    operator_input = tmp_path / "operator.pem"
    operator_input.write_text("healthy operator input")
    (tmp_path / ".env.prod").write_text(f"FS_TLS_CERTIFICATE_PATH={operator_input}\n")
    mirror_command = ("openssl", "x509", "-enddate", "-noout", "-in", str(mirror))
    gateway = _revision_gateway(
        tmp_path,
        {
            mirror_command: (1, "") if unreadable else (0, "notAfter=Oct  1 12:00:00 2000 GMT\n"),
            ("openssl",): (0, "notAfter=Oct  1 12:00:00 2027 GMT\n"),
        },
    )
    probe = InstallationDoctorProbe(
        root=tmp_path,
        gateway=gateway,
        state=gateway.state_store,
        config=_config(tls_mode="provided", certificate_path=str(operator_input)),
        log=None,
    )
    finding = _certificate_expiry(probe)
    assert finding.status == "fail"
    assert str(mirror) in finding.cause
    assert ("missing or cannot be read" if unreadable else "expired") in finding.cause
    assert f"Replace {operator_input}" in finding.next_step
    assert gateway.process.commands == [list(mirror_command)]
