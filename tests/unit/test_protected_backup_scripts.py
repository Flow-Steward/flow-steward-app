from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.application.tooling.db.protected_export as protected_export
from core.application.tooling.db.protected_export import decrypt_file, encrypt_file

ROOT = Path(__file__).parent.parent.parent
PROTECT_EXPORT = ROOT / "core" / "application" / "tooling" / "db" / "protected_export.py"
RUNTIME_ENV = ROOT / "core" / "infrastructure" / "runtime_env.py"
DOCKER_ENV = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "docker_compose_env.sh"
DB_BACKUP = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "db_backup.sh"
DB_RESTORE = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "db_restore.sh"
DB_VALIDATE = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "db_backup_validate.sh"
OPS_SCHEDULE = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "ops_schedule.sh"


def _write_fake_docker(
    bin_dir: Path,
    capture_file: Path,
    dump_payload: bytes,
    *,
    command_log: Path | None = None,
    fail_pg_dump: bool = False,
    fail_pg_restore_list: bool = False,
    pg_restore_list_closes_early: bool = False,
    running_services: tuple[str, ...] = (),
    pg_restore_stops_reading_early: bool = False,
) -> None:
    docker_path = bin_dir / "docker"
    log_lines = []
    if command_log is not None:
        log_lines.append(f"printf '%s\\n' \"$args\" >> {shlex.quote(str(command_log))}")
    pg_restore_list_result = "exit 1" if fail_pg_restore_list else "exit 0"
    pg_restore_list_input = (
        "head -c 1024 > /dev/null" if pg_restore_list_closes_early else "cat > /dev/null"
    )
    pg_dump_result = (
        'echo "pg_dump failed for postgresql://operator:unsafe@example/db" >&2\n  exit 1'
        if fail_pg_dump
        else f"printf '%s' {shlex.quote(dump_payload.decode('utf-8'))}\n  exit 0"
    )
    protected_export_command = (
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(PROTECT_EXPORT))} "${{!#}}"'
    )
    docker_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'args="$*"',
                *log_lines,
                'if [[ "$args" == *"protected_export.py"* ]]; then',
                f"  {protected_export_command}",
                "fi",
                'if [[ "$args" == *"pg_dump"* ]]; then',
                f"  {pg_dump_result}",
                "fi",
                'if [[ "$args" == *"sh -ceu"* && "$args" == *"mktemp /tmp/flow-steward-restore"* ]]; then',
                "  cat > /dev/null",
                f"  {pg_restore_list_result}",
                "fi",
                'if [[ "$args" == *"pg_restore -l"* ]]; then',
                f"  {pg_restore_list_input}",
                f"  {pg_restore_list_result}",
                "fi",
                'if [[ "$args" == *"up -d --no-deps --wait pg-fs"* ]]; then',
                "  exit 0",
                "fi",
                'if [[ "$args" == *"ps --status running --services"* ]]; then',
                f"  printf '%s\\n' {' '.join(shlex.quote(name) for name in ('pg-fs', *running_services))}",
                "  exit 0",
                "fi",
                'if [[ "$args" == "compose "*" stop "* ]]; then',
                "  exit 0",
                "fi",
                'if [[ "$args" == *" dropdb "* || "$args" == *" createdb "* ]]; then',
                "  exit 0",
                "fi",
                'if [[ "$args" == *"FLOW_STEWARD_AGE_RESTORE_REPAIR"* ]]; then',
                "  exit 0",
                "fi",
                # the restore that takes the whole stream first, then reads the file
                'if [[ "$args" == *"mktemp /tmp/flow-steward-apply"* ]]; then',
                f"  cat > {shlex.quote(str(capture_file))}",
                "  exit 0",
                "fi",
                # AC17-R164: pg_restore stops reading a custom-format dump at its last
                # entry, before the stream feeding it has ended
                'if [[ "$args" == *"pg_restore"* ]]; then',
                (
                    f"  head -c 4 > {shlex.quote(str(capture_file))}"
                    if pg_restore_stops_reading_early
                    else f"  cat > {shlex.quote(str(capture_file))}"
                ),
                "  exit 0",
                "fi",
                'if [[ "$args" == *"psql"* ]]; then',
                f"  cat > {shlex.quote(str(capture_file))}",
                "  exit 0",
                "fi",
                'echo "unexpected docker invocation: $args" >&2',
                "exit 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    docker_path.chmod(0o755)


def _write_python_shim(path: Path, log_file: Path, label: str) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"printf '%s\\n' {shlex.quote(label)} >> {shlex.quote(str(log_file))}",
                f'exec {shlex.quote(sys.executable)} "$@"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _copy_backup_script_tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "repo"
    scripts_dir = root / "core" / "entrypoints" / "cli" / "scripts"
    tooling_dir = root / "core" / "application" / "tooling" / "db"
    infrastructure_dir = root / "core" / "infrastructure"
    scripts_dir.mkdir(parents=True)
    tooling_dir.mkdir(parents=True)
    infrastructure_dir.mkdir(parents=True)

    script_sources = {
        "docker_compose_env": DOCKER_ENV,
        "db_backup": DB_BACKUP,
        "db_restore": DB_RESTORE,
        "db_backup_validate": DB_VALIDATE,
        "ops_schedule": OPS_SCHEDULE,
    }
    paths = {"root": root, "protected_export": tooling_dir / "protected_export.py"}
    for name, source in script_sources.items():
        target = scripts_dir / source.name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)
        paths[name] = target
    paths["protected_export"].write_bytes(PROTECT_EXPORT.read_bytes())
    paths["protected_export"].chmod(0o755)
    (infrastructure_dir / "runtime_env.py").write_bytes(RUNTIME_ENV.read_bytes())
    return paths


def _write_compose_env(root: Path, env_file: str) -> Path:
    stack_dir = root / ".flow-steward"
    stack_dir.mkdir(parents=True, exist_ok=True)
    stack_env = stack_dir / "compose.env"
    stack_env.write_text(
        "\n".join(
            [
                f"FS_ENV_FILE={env_file}",
                "FS_COMPOSE_FILES=infra/docker/docker-compose.yaml",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return stack_env


def _encrypt_payload(
    payload: bytes, secret: str, *, protect_export: Path = PROTECT_EXPORT
) -> bytes:
    env = os.environ.copy()
    env["FS_BACKUP_ENCRYPTION_KEY"] = secret
    return subprocess.run(
        [sys.executable, str(protect_export), "encrypt"],
        input=payload,
        capture_output=True,
        check=True,
        cwd=protect_export.parents[4],
        env=env,
    ).stdout


def _decrypt_payload(
    payload: bytes,
    secret: str,
    *,
    protect_export: Path = PROTECT_EXPORT,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["FS_BACKUP_ENCRYPTION_KEY"] = secret
    return subprocess.run(
        [sys.executable, str(protect_export), "decrypt"],
        input=payload,
        capture_output=True,
        check=check,
        cwd=protect_export.parents[4],
        env=env,
    )


def _script_env(fake_bin: Path, tmp_path: Path, *, secret: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env.pop("FS_SECRET_KEY", None)
    env.pop("ENCRYPTION_KEY", None)
    env.pop("POSTGRES_PASSWORD", None)
    if secret is None:
        env.pop("FS_BACKUP_ENCRYPTION_KEY", None)
    else:
        env["FS_BACKUP_ENCRYPTION_KEY"] = secret
    env["POSTGRES_USER"] = "flow_steward"
    env["POSTGRES_DB"] = "flow_steward"
    env["PYTHON_BIN"] = sys.executable
    env["TMPDIR"] = str(tmp_path)
    env["FS_ARTIFACT_ROOT"] = str(tmp_path)
    # The backup command may read its dedicated key from FS_ENV_FILE.  Point
    # tests at an isolated, absent file so the developer's real .env cannot
    # make a missing-key case pass accidentally.
    env["FS_ENV_FILE"] = str(tmp_path / ".env")
    # The script tree's own .flow-steward/compose.env is the installation under
    # test; an inherited override (the audit container sets one) would hide it.
    env.pop("FS_STACK_ENV_FILE", None)
    return env


def test_protected_export_cli_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_BACKUP_ENCRYPTION_KEY", "unit-test-secret-key")
    payload = b"custom-format-payload"

    encrypted = subprocess.run(
        [sys.executable, str(PROTECT_EXPORT), "encrypt"],
        input=payload,
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout

    assert payload not in encrypted
    assert encrypted.startswith(b"protected-export:")

    decrypted = subprocess.run(
        [sys.executable, str(PROTECT_EXPORT), "decrypt"],
        input=encrypted,
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout

    assert decrypted == payload


def test_decrypt_file_never_exposes_tampered_multimegabyte_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = tmp_path / "large.dump"
    protected = tmp_path / "large.dump.enc"
    destination = tmp_path / "tampered-output.dump"
    plaintext.write_bytes(b"PGDMP" + b"x" * (3 * 1024 * 1024))
    destination.write_bytes(b"")
    encrypt_file(plaintext, protected, key_source=lambda: "dedicated-key")
    with protected.open("r+b") as payload:
        payload.seek(-1, os.SEEK_END)
        last_tag_byte = payload.read(1)[0]
        payload.seek(-1, os.SEEK_END)
        payload.write(bytes([last_tag_byte ^ 1]))

    class ObservedDestination(io.BytesIO):
        max_observed_bytes = 0

        def write(self, payload: bytes) -> int:
            written = super().write(payload)
            self.max_observed_bytes = max(self.max_observed_bytes, self.tell())
            return written

    observed = ObservedDestination()
    original_open = Path.open

    def observed_open(path: Path, mode: str = "r", *args, **kwargs):
        if path == destination and mode == "wb":
            return observed
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)

    with pytest.raises(RuntimeError, match="authentication failed"):
        decrypt_file(protected, destination, key_source=lambda: "dedicated-key")

    assert observed.max_observed_bytes == 0
    assert destination.stat().st_size == 0


def test_stdio_decrypt_uses_owner_only_anonymous_temps_under_canonical_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_payload = io.BytesIO()
    protected_export.encrypt_stream(
        io.BytesIO(b"PGDMP protected payload"),
        protected_payload,
        key_source=lambda: "dedicated-key",
    )
    observed_directories: list[Path | None] = []
    observed_modes: list[int] = []
    real_mkstemp = protected_export.mkstemp
    real_fchmod = protected_export.os.fchmod

    def observed_mkstemp(*args, **kwargs):
        raw_directory = kwargs.get("dir")
        observed_directories.append(Path(raw_directory) if raw_directory else None)
        return real_mkstemp(*args, **kwargs)

    def observed_fchmod(descriptor: int, mode: int) -> None:
        observed_modes.append(mode)
        real_fchmod(descriptor, mode)

    output = io.BytesIO()
    monkeypatch.setenv("FS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("FS_BACKUP_ENCRYPTION_KEY", "dedicated-key")
    monkeypatch.setattr(protected_export, "mkstemp", observed_mkstemp)
    monkeypatch.setattr(protected_export.os, "fchmod", observed_fchmod)
    monkeypatch.setattr(
        protected_export.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(protected_payload.getvalue())),
    )
    monkeypatch.setattr(
        protected_export.sys,
        "stdout",
        SimpleNamespace(buffer=output),
    )

    protected_export._run_stdio_mode("decrypt")

    backups = (tmp_path / "backups").resolve()
    assert output.getvalue() == b"PGDMP protected payload"
    assert observed_directories == [backups, backups]
    assert observed_modes == [0o600, 0o600]
    assert backups.stat().st_mode & 0o777 == 0o700
    assert not list(backups.glob(".protected-export-*"))


def test_db_backup_writes_encrypted_export_and_restore_decrypts_it(tmp_path, monkeypatch):
    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["FS_BACKUP_ENCRYPTION_KEY"] = "unit-test-secret-key"
    env["POSTGRES_USER"] = "flow_steward"
    env["POSTGRES_DB"] = "flow_steward"
    env["PYTHON_BIN"] = sys.executable
    env["TMPDIR"] = str(tmp_path)
    env["FS_ARTIFACT_ROOT"] = str(tmp_path)

    backup_file = tmp_path / "backups" / "backup.dump.enc"
    subprocess.run(
        [str(DB_BACKUP), str(backup_file)],
        check=True,
        cwd=ROOT,
        env=env,
    )

    encrypted_payload = backup_file.read_bytes()
    assert dump_payload not in encrypted_payload
    assert encrypted_payload.startswith(b"protected-export:")

    subprocess.run(
        [
            str(DB_RESTORE),
            str(backup_file),
            "--target-compose-project",
            "flow-steward",
            "--confirm",
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )

    assert capture_file.read_bytes() == dump_payload
    assert not list(tmp_path.glob("db_backup.*"))
    assert not list(tmp_path.glob("db_restore.*"))


def test_db_backup_loads_secret_from_env_file_selected_by_compose_env(tmp_path):
    paths = _copy_backup_script_tree(tmp_path)
    env_file = paths["root"] / "config" / "ops.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("FS_BACKUP_ENCRYPTION_KEY=file-secret-key\n", encoding="utf-8")
    _write_compose_env(paths["root"], "config/ops.env")

    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = _script_env(fake_bin, tmp_path, secret=None)
    backup_file = tmp_path / "backups" / "backup.dump.enc"
    subprocess.run(
        [str(paths["db_backup"]), str(backup_file)],
        check=True,
        cwd=paths["root"],
        env=env,
    )

    encrypted_payload = backup_file.read_bytes()
    assert encrypted_payload.startswith(b"protected-export:")
    decrypted = _decrypt_payload(
        encrypted_payload,
        "file-secret-key",
        protect_export=paths["protected_export"],
    ).stdout
    assert decrypted == dump_payload


def test_db_backup_keeps_process_secret_precedence_over_env_file(tmp_path):
    paths = _copy_backup_script_tree(tmp_path)
    env_file = paths["root"] / "ops.env"
    env_file.write_text("FS_BACKUP_ENCRYPTION_KEY=file-secret-key\n", encoding="utf-8")
    _write_compose_env(paths["root"], "ops.env")

    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = _script_env(fake_bin, tmp_path, secret="process-secret-key")
    backup_file = tmp_path / "backups" / "backup.dump.enc"
    subprocess.run(
        [str(paths["db_backup"]), str(backup_file)],
        check=True,
        cwd=paths["root"],
        env=env,
    )

    encrypted_payload = backup_file.read_bytes()
    assert (
        _decrypt_payload(
            encrypted_payload,
            "process-secret-key",
            protect_export=paths["protected_export"],
        ).stdout
        == dump_payload
    )
    assert (
        _decrypt_payload(
            encrypted_payload,
            "file-secret-key",
            protect_export=paths["protected_export"],
            check=False,
        ).returncode
        != 0
    )


def test_db_backup_treats_env_file_secret_as_data_without_shell_execution(tmp_path):
    paths = _copy_backup_script_tree(tmp_path)
    marker = tmp_path / "env-file-was-executed"
    literal_secret = f"$(touch {marker})"
    env_file = paths["root"] / "ops.env"
    env_file.write_text(f'FS_BACKUP_ENCRYPTION_KEY="{literal_secret}"\n', encoding="utf-8")
    _write_compose_env(paths["root"], "ops.env")

    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = _script_env(fake_bin, tmp_path, secret=None)
    backup_file = tmp_path / "backups" / "backup.dump.enc"
    subprocess.run(
        [str(paths["db_backup"]), str(backup_file)],
        check=True,
        cwd=paths["root"],
        env=env,
    )

    assert not marker.exists()
    assert (
        _decrypt_payload(
            backup_file.read_bytes(),
            literal_secret,
            protect_export=paths["protected_export"],
        ).stdout
        == dump_payload
    )


def test_db_restore_auto_detects_protected_export_without_enc_suffix(tmp_path):
    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["FS_BACKUP_ENCRYPTION_KEY"] = "unit-test-secret-key"
    env["POSTGRES_USER"] = "flow_steward"
    env["POSTGRES_DB"] = "flow_steward"
    env["PYTHON_BIN"] = sys.executable
    env["FS_ARTIFACT_ROOT"] = str(tmp_path)

    protected_dump = tmp_path / "backups" / "scheduled.dump"
    subprocess.run(
        [str(DB_BACKUP), str(protected_dump)],
        check=True,
        cwd=ROOT,
        env=env,
    )

    subprocess.run(
        [
            str(DB_RESTORE),
            str(protected_dump),
            "--target-compose-project",
            "flow-steward",
            "--confirm",
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )

    assert capture_file.read_bytes() == dump_payload


@pytest.mark.parametrize(
    ("compose_files", "expected_service"),
    [
        ("infra/docker/docker-compose.prod.yaml", "fs-app"),
        ("infra/docker/docker-compose.compact.yaml", "compact-runtime"),
    ],
)
def test_db_restore_decrypts_inside_the_selected_application_container(
    tmp_path,
    compose_files,
    expected_service,
):
    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    command_log = tmp_path / "docker.log"
    python_log = tmp_path / "python.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        capture_file,
        dump_payload,
        command_log=command_log,
    )
    host_python = fake_bin / "host-python"
    _write_python_shim(host_python, python_log, "host-python-invoked")
    protected_dump = tmp_path / "backups" / "protected.dump.enc"
    protected_dump.parent.mkdir()
    protected_dump.write_bytes(_encrypt_payload(dump_payload, "dedicated-key"))
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")
    env["PYTHON_BIN"] = str(host_python)
    stack_env = tmp_path / "compose.env"
    stack_env.write_text(f"FS_COMPOSE_FILES={compose_files}\n", encoding="utf-8")
    env["FS_STACK_ENV_FILE"] = str(stack_env)

    subprocess.run(
        [
            str(DB_RESTORE),
            str(protected_dump),
            "--target-compose-project",
            "flow-steward",
            "--confirm",
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )

    commands = command_log.read_text(encoding="utf-8")
    assert "compose" in commands
    # AC17-R161: after `stack stop` the database was down and the restore failed
    started = commands.index("up -d --no-deps --wait pg-fs")
    assert started < commands.index("exec -T pg-fs")
    assert "run --rm --no-deps -T" in commands
    assert "--pull never" in commands
    assert "--no-build" not in commands
    assert "-e FS_ARTIFACT_ROOT=/tmp" in commands
    assert f"--entrypoint python {expected_service}" in commands
    assert "/app/core/application/tooling/db/protected_export.py decrypt" in commands
    assert "exec -T pg-fs dropdb --if-exists --force" in commands
    assert "--maintenance-db=postgres" in commands
    assert "exec -T pg-fs createdb --template=template0" in commands
    apply_start = commands.index("mktemp /tmp/flow-steward-apply")
    restore_command = commands[apply_start : commands.index("FLOW_STEWARD_AGE_RESTORE_REPAIR")]
    assert "exec -T pg-fs sh -ceu" in commands
    assert "pg_restore -v --no-owner --no-privileges" in restore_command
    assert "--clean" not in restore_command
    assert "--if-exists" not in restore_command
    assert "FLOW_STEWARD_AGE_RESTORE_REPAIR" in commands
    assert not python_log.exists()
    assert capture_file.read_bytes() == dump_payload


def test_db_restore_fully_consumes_large_protected_dump_before_destructive_reset(
    tmp_path: Path,
) -> None:
    dump_payload = b"PGDMP" + b"x" * (4 * 1024 * 1024)
    protected_dump = tmp_path / "backups" / "large.dump.enc"
    protected_dump.parent.mkdir()
    protected_dump.write_bytes(_encrypt_payload(dump_payload, "dedicated-key"))
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        dump_payload,
        command_log=command_log,
        pg_restore_list_closes_early=True,
    )

    completed = subprocess.run(
        [
            str(DB_RESTORE),
            str(protected_dump),
            "--target-compose-project",
            "flow-steward",
            "--confirm",
        ],
        cwd=ROOT,
        env=_script_env(fake_bin, tmp_path, secret="dedicated-key"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    commands = command_log.read_text(encoding="utf-8")
    assert "mktemp /tmp/flow-steward-restore" in commands
    assert "trap" in commands
    assert "dropdb" in commands


def test_db_restore_rejects_a_mismatched_explicit_compose_target(tmp_path):
    backup = tmp_path / "protected.dump.enc"
    backup.write_bytes(_encrypt_payload(b"pg-dump-payload", "dedicated-key"))
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        b"pg-dump-payload",
        command_log=command_log,
    )

    completed = subprocess.run(
        [
            str(DB_RESTORE),
            str(backup),
            "--target-compose-project",
            "wrong-project",
            "--confirm",
        ],
        cwd=ROOT,
        env=_script_env(fake_bin, tmp_path, secret="dedicated-key"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "Restore target mismatch" in completed.stderr
    assert not command_log.exists()


def test_db_restore_refuses_postgresql_system_database_before_destructive_reset(tmp_path):
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"PGDMP custom-format")
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        b"pg-dump-payload",
        command_log=command_log,
    )
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")
    env["POSTGRES_DB"] = "postgres"

    completed = subprocess.run(
        [
            str(DB_RESTORE),
            str(backup),
            "--target-compose-project",
            "flow-steward",
            "--confirm",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "Refusing to replace PostgreSQL system database" in completed.stderr
    commands = command_log.read_text(encoding="utf-8")
    assert "pg_restore -l" in commands
    assert "dropdb" not in commands
    assert "createdb" not in commands


def test_db_backup_prefers_python_bin_over_fs_python_bin(tmp_path):
    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    python_log = tmp_path / "python.log"
    python_bin = fake_bin / "python-bin"
    fs_python_bin = fake_bin / "fs-python-bin"
    _write_python_shim(python_bin, python_log, "PYTHON_BIN")
    _write_python_shim(fs_python_bin, python_log, "FS_PYTHON_BIN")

    env = _script_env(fake_bin, tmp_path, secret="unit-test-secret-key")
    env["PYTHON_BIN"] = str(python_bin)
    env["FS_PYTHON_BIN"] = str(fs_python_bin)

    subprocess.run(
        [str(DB_BACKUP), str(tmp_path / "backups" / "backup.dump.enc")],
        check=True,
        cwd=ROOT,
        env=env,
    )

    assert python_log.read_text(encoding="utf-8").splitlines() == [
        "PYTHON_BIN",
        "PYTHON_BIN",
    ]


def test_ops_schedule_backup_uses_fs_python_bin_when_python_bin_unset(tmp_path):
    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    python_log = tmp_path / "python.log"
    fs_python_bin = fake_bin / "fs-python-bin"
    _write_python_shim(fs_python_bin, python_log, "FS_PYTHON_BIN")

    ops_dir = tmp_path / "ops"
    env = _script_env(fake_bin, tmp_path, secret="unit-test-secret-key")
    env.pop("PYTHON_BIN", None)
    env["FS_PYTHON_BIN"] = str(fs_python_bin)
    env["FS_ARTIFACT_ROOT"] = str(ops_dir)

    subprocess.run(
        [str(OPS_SCHEDULE), "backup"],
        check=True,
        cwd=ROOT,
        env=env,
    )

    backup_files = list((ops_dir / "backups").glob("*.dump.enc"))
    assert len(backup_files) == 1
    assert python_log.read_text(encoding="utf-8").splitlines() == [
        "FS_PYTHON_BIN",
        "FS_PYTHON_BIN",
    ]


def test_ops_schedule_uses_application_modules_for_internal_ops_tasks():
    source = OPS_SCHEDULE.read_text(encoding="utf-8")

    assert "core.entrypoints.cli \\\n    tooling" not in source
    assert "core.application.tooling.ops.retention_cleanup" in source
    assert "core.application.tooling.ops.prune_workspace_artifacts" in source


def test_db_backup_validate_accepts_protected_export_and_lists_dump(tmp_path):
    dump_payload = b"pg-dump-payload"
    backup_file = tmp_path / "backup.dump.enc"
    backup_file.write_bytes(_encrypt_payload(dump_payload, "unit-test-secret-key"))

    capture_file = tmp_path / "restore.bin"
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload, command_log=command_log)

    env = _script_env(fake_bin, tmp_path, secret="unit-test-secret-key")
    subprocess.run(
        [str(DB_VALIDATE), str(backup_file)],
        check=True,
        cwd=ROOT,
        env=env,
    )

    commands = command_log.read_text(encoding="utf-8")
    assert "pg_restore -l" in commands
    assert "db_restore.sh" not in commands
    assert "psql" not in commands
    assert "pg_restore -d" not in commands
    assert not list(tmp_path.glob("db_backup_validate.*"))


def test_db_backup_validate_loads_secret_from_env_file(tmp_path):
    paths = _copy_backup_script_tree(tmp_path)
    env_file = paths["root"] / "ops.env"
    env_file.write_text("FS_BACKUP_ENCRYPTION_KEY=file-secret-key\n", encoding="utf-8")
    _write_compose_env(paths["root"], "ops.env")

    dump_payload = b"pg-dump-payload"
    backup_file = tmp_path / "backup.dump.enc"
    backup_file.write_bytes(
        _encrypt_payload(
            dump_payload,
            "file-secret-key",
            protect_export=paths["protected_export"],
        )
    )

    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = _script_env(fake_bin, tmp_path, secret=None)
    subprocess.run(
        [str(paths["db_backup_validate"]), str(backup_file)],
        check=True,
        cwd=paths["root"],
        env=env,
    )

    assert not list(tmp_path.glob("db_backup_validate.*"))


def test_db_backup_validate_keeps_process_secret_precedence_over_env_file(tmp_path):
    paths = _copy_backup_script_tree(tmp_path)
    env_file = paths["root"] / "ops.env"
    env_file.write_text("FS_BACKUP_ENCRYPTION_KEY=file-secret-key\n", encoding="utf-8")
    _write_compose_env(paths["root"], "ops.env")

    dump_payload = b"pg-dump-payload"
    backup_file = tmp_path / "backup.dump.enc"
    backup_file.write_bytes(
        _encrypt_payload(
            dump_payload,
            "process-secret-key",
            protect_export=paths["protected_export"],
        )
    )

    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    env = _script_env(fake_bin, tmp_path, secret="process-secret-key")
    subprocess.run(
        [str(paths["db_backup_validate"]), str(backup_file)],
        check=True,
        cwd=paths["root"],
        env=env,
    )


def test_db_backup_validate_rejects_invalid_protected_export_and_cleans_temp(tmp_path):
    backup_file = tmp_path / "invalid.dump.enc"
    backup_file.write_bytes(b"not-a-protected-export")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", b"pg-dump-payload")

    env = _script_env(fake_bin, tmp_path, secret="unit-test-secret-key")
    completed = subprocess.run(
        [str(DB_VALIDATE), str(backup_file)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "protected export" in completed.stderr
    assert not list(tmp_path.glob("db_backup_validate.*"))


def test_db_backup_validate_cleans_temp_when_pg_restore_list_fails(tmp_path):
    backup_file = tmp_path / "backup.dump.enc"
    backup_file.write_bytes(_encrypt_payload(b"not-a-pg-dump", "unit-test-secret-key"))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        b"pg-dump-payload",
        fail_pg_restore_list=True,
    )

    env = _script_env(fake_bin, tmp_path, secret="unit-test-secret-key")
    completed = subprocess.run(
        [str(DB_VALIDATE), str(backup_file)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "readable PostgreSQL custom-format dump" in completed.stderr
    assert "db_backup_validate." not in completed.stdout + completed.stderr
    assert not list(tmp_path.glob("db_backup_validate.*"))


def test_ops_schedule_backup_writes_dump_enc_artifact(tmp_path):
    dump_payload = b"pg-dump-payload"
    capture_file = tmp_path / "restore.bin"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, capture_file, dump_payload)

    ops_dir = tmp_path / "ops"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["FS_BACKUP_ENCRYPTION_KEY"] = "unit-test-secret-key"
    env["POSTGRES_USER"] = "flow_steward"
    env["POSTGRES_DB"] = "flow_steward"
    env["PYTHON_BIN"] = sys.executable
    env["TMPDIR"] = str(tmp_path)
    env["FS_ARTIFACT_ROOT"] = str(ops_dir)

    subprocess.run(
        [str(OPS_SCHEDULE), "backup"],
        check=True,
        cwd=ROOT,
        env=env,
    )

    backup_files = list((ops_dir / "backups").glob("*.dump.enc"))
    assert len(backup_files) == 1
    assert backup_files[0].name.startswith("flow_steward_")
    assert backup_files[0].stat().st_mode & 0o777 == 0o600
    assert not list((ops_dir / "backups").glob("*.dump"))


def test_protected_export_never_falls_back_to_other_secrets(tmp_path):
    env = os.environ.copy()
    env.pop("FS_BACKUP_ENCRYPTION_KEY", None)
    env["FS_SECRET_KEY"] = "must-not-be-used"
    env["ENCRYPTION_KEY"] = "must-not-be-used"
    env["POSTGRES_PASSWORD"] = "must-not-be-used"

    completed = subprocess.run(
        [sys.executable, str(PROTECT_EXPORT), "encrypt"],
        input=b"pg-dump",
        capture_output=True,
        check=False,
        cwd=ROOT,
        env=env,
    )

    assert completed.returncode != 0
    assert b"FS_BACKUP_ENCRYPTION_KEY is required" in completed.stderr
    assert b"must-not-be-used" not in completed.stderr


def test_db_backup_missing_dedicated_key_blocks_without_incomplete_output(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", b"pg-dump-payload")
    env = _script_env(fake_bin, tmp_path, secret=None)
    # The negative case must not inherit a developer-local compose secret.
    env["FS_STACK_ENV_FILE"] = str(tmp_path / "compose.env")
    env["FS_SECRET_KEY"] = "must-not-be-used"
    env["ENCRYPTION_KEY"] = "must-not-be-used"
    env["POSTGRES_PASSWORD"] = "must-not-be-used"
    output = tmp_path / "backups" / "backup.dump.enc"

    completed = subprocess.run(
        [str(DB_BACKUP), str(output)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "FS_BACKUP_ENCRYPTION_KEY is required" in completed.stderr
    assert "must-not-be-used" not in completed.stdout + completed.stderr
    assert not output.exists()
    assert not list((tmp_path / "backups").glob(".db_backup*"))
    failure_logs = list((tmp_path / "logs" / "runtime").glob("backup_failure_*.log"))
    assert len(failure_logs) == 1
    assert failure_logs[0].stat().st_mode & 0o777 == 0o600
    assert "must-not-be-used" not in failure_logs[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fail_pg_dump", "fail_pg_restore_list", "expected"),
    [
        (True, False, "pg_dump failed"),
        (False, True, "readable PostgreSQL custom-format dump"),
    ],
)
def test_db_backup_process_failure_removes_incomplete_output_and_plaintext(
    tmp_path,
    fail_pg_dump,
    fail_pg_restore_list,
    expected,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        b"pg-dump-payload",
        fail_pg_dump=fail_pg_dump,
        fail_pg_restore_list=fail_pg_restore_list,
    )
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")
    output = tmp_path / "backups" / "backup.dump.enc"

    completed = subprocess.run(
        [str(DB_BACKUP), str(output)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert expected in completed.stdout + completed.stderr
    assert "operator:unsafe" not in completed.stdout + completed.stderr
    assert not output.exists()
    assert not list((tmp_path / "backups").glob(".db_backup*"))
    failure_logs = list((tmp_path / "logs" / "runtime").glob("backup_failure_*.log"))
    assert len(failure_logs) == 1
    assert "operator:unsafe" not in failure_logs[0].read_text(encoding="utf-8")


def test_db_backup_validate_wrong_key_and_truncation_leave_no_plaintext(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", b"pg-dump-payload")
    backup = tmp_path / "backup.dump.enc"
    protected = _encrypt_payload(b"pg-dump-payload", "correct-key")

    for payload, key in ((protected, "wrong-key"), (protected[:-12], "correct-key")):
        backup.write_bytes(payload)
        completed = subprocess.run(
            [str(DB_VALIDATE), str(backup)],
            cwd=ROOT,
            env=_script_env(fake_bin, tmp_path, secret=key),
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode != 0
        backups = tmp_path / "backups"
        assert not list(backups.glob(".db_backup_validate*"))
        assert not list(backups.glob(".protected-export-*"))


def test_db_restore_is_operator_only_and_requires_explicit_dedicated_key(tmp_path):
    source = DB_RESTORE.read_text(encoding="utf-8")
    backup = tmp_path / "backup.dump.enc"
    backup.write_bytes(_encrypt_payload(b"pg-dump-payload", "dedicated-key"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", b"pg-dump-payload")

    completed = subprocess.run(
        [str(DB_RESTORE), str(backup)],
        cwd=ROOT,
        env=_script_env(fake_bin, tmp_path, secret=None),
        text=True,
        capture_output=True,
        check=False,
    )

    assert "OPERATOR RECOVERY ONLY" in source
    assert "not registered in the public Flow Steward CLI" in source
    assert completed.returncode != 0
    assert "FS_BACKUP_ENCRYPTION_KEY must be exported explicitly" in completed.stderr


@pytest.mark.parametrize(
    ("name", "payload"),
    [("plain.sql", b"select 1;"), ("plain.dump", b"PGDMP custom-format")],
)
def test_db_restore_requires_dedicated_key_before_any_destructive_restore(
    tmp_path,
    name,
    payload,
):
    backup = tmp_path / name
    backup.write_bytes(payload)
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        b"pg-dump-payload",
        command_log=command_log,
    )

    completed = subprocess.run(
        [str(DB_RESTORE), str(backup)],
        cwd=ROOT,
        env=_script_env(fake_bin, tmp_path, secret=None),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "FS_BACKUP_ENCRYPTION_KEY must be exported explicitly" in completed.stderr
    assert not command_log.exists()


def test_db_backup_rejects_output_outside_canonical_backups(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", b"pg-dump-payload")

    completed = subprocess.run(
        [str(DB_BACKUP), str(tmp_path / "outside.dump.enc")],
        cwd=ROOT,
        env=_script_env(fake_bin, tmp_path, secret="dedicated-key"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "under" in completed.stderr
    assert "backups" in completed.stderr
    assert not (tmp_path / "outside.dump.enc").exists()


def test_db_backup_validate_temp_is_always_under_canonical_backups(tmp_path):
    backup = tmp_path / "outside-input.dump.enc"
    backup.write_bytes(_encrypt_payload(b"pg-dump-payload", "dedicated-key"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", b"pg-dump-payload")
    real_mktemp = shutil.which("mktemp") or "/usr/bin/mktemp"
    mktemp_log = tmp_path / "mktemp.log"
    shim = fake_bin / "mktemp"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$1\" >> {shlex.quote(str(mktemp_log))}\n"
        f'exec {shlex.quote(real_mktemp)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    subprocess.run(
        [str(DB_VALIDATE), str(backup)],
        cwd=ROOT,
        env=_script_env(fake_bin, tmp_path, secret="dedicated-key"),
        check=True,
    )

    templates = mktemp_log.read_text(encoding="utf-8").splitlines()
    assert templates
    assert all(str(tmp_path / "backups") in template for template in templates)


_RESTORE_TARGET = ("--target-compose-project", "flow-steward", "--confirm")


@pytest.mark.parametrize(
    ("compose_files", "writers", "idle"),
    [
        ("infra/docker/docker-compose.compact.yaml", ("compact-runtime",), ()),
        (
            "infra/docker/docker-compose.yaml:infra/docker/docker-compose.prod.yaml",
            ("control-plane", "fs-app", "fs-worker"),
            ("fs-mcp",),
        ),
    ],
    ids=["compact", "scalable"],
)
def test_db_restore_stops_the_running_writers_before_replacing_the_database(
    tmp_path: Path, compose_files: str, writers: tuple[str, ...], idle: tuple[str, ...]
) -> None:
    """AC17-R162: on Compact `stack stop` is refused, so the runbook had no way to stop
    the writers, and the restore replaced the database under a runtime still writing."""
    dump_payload = b"pg-dump-payload"
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        dump_payload,
        command_log=command_log,
        running_services=writers,
    )
    protected_dump = tmp_path / "backups" / "protected.dump.enc"
    protected_dump.parent.mkdir()
    protected_dump.write_bytes(_encrypt_payload(dump_payload, "dedicated-key"))
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")
    stack_env = tmp_path / "compose.env"
    stack_env.write_text(f"FS_COMPOSE_FILES={compose_files}\n", encoding="utf-8")
    env["FS_STACK_ENV_FILE"] = str(stack_env)

    result = subprocess.run(
        [str(DB_RESTORE), str(protected_dump), *_RESTORE_TARGET],
        check=True,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    commands = command_log.read_text(encoding="utf-8").splitlines()
    stop = next(index for index, line in enumerate(commands) if " stop " in f"{line} ")
    dropped = next(index for index, line in enumerate(commands) if "dropdb" in line)
    assert stop < dropped
    stopped = set(commands[stop].split(" stop ", 1)[1].split())
    assert stopped == set(writers)  # pg-fs keeps running; a service not running is not named
    assert not stopped & set(idle)
    assert not any(" up " in f"{line} " and "pg-fs" not in line for line in commands)
    assert "left stopped; run ./install.sh" in result.stderr
    assert all(service in result.stderr for service in writers)


def test_db_restore_with_the_writers_already_stopped_stops_nothing(tmp_path: Path) -> None:
    """The green direction: the runbook's Scalable sequence stops the stack first."""
    dump_payload = b"pg-dump-payload"
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin, tmp_path / "restore.bin", dump_payload, command_log=command_log)
    protected_dump = tmp_path / "backups" / "protected.dump.enc"
    protected_dump.parent.mkdir()
    protected_dump.write_bytes(_encrypt_payload(dump_payload, "dedicated-key"))
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")

    result = subprocess.run(
        [str(DB_RESTORE), str(protected_dump), *_RESTORE_TARGET],
        check=True,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert " stop " not in command_log.read_text(encoding="utf-8")
    assert "left stopped" not in result.stderr


def _restore_with_a_reader_that_stops_early(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    # large enough that the decryption is still writing when pg_restore stops reading
    dump_payload = b"PGDMP" + b"x" * (2 * 1024 * 1024)
    command_log = tmp_path / "docker.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin,
        tmp_path / "restore.bin",
        dump_payload,
        command_log=command_log,
        running_services=("compact-runtime",),
        pg_restore_stops_reading_early=True,
    )
    protected_dump = tmp_path / "backups" / "protected.dump.enc"
    protected_dump.parent.mkdir()
    protected_dump.write_bytes(_encrypt_payload(dump_payload, "dedicated-key"))
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")
    stack_env = tmp_path / "compose.env"
    stack_env.write_text(
        "FS_COMPOSE_FILES=infra/docker/docker-compose.compact.yaml\n", encoding="utf-8"
    )
    env["FS_STACK_ENV_FILE"] = str(stack_env)
    return subprocess.run(
        [str(DB_RESTORE), str(protected_dump), *_RESTORE_TARGET],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_a_compact_restore_repairs_the_graph_even_when_pg_restore_stops_reading_early(
    tmp_path: Path,
) -> None:
    """AC17-R164: on Compact the decryption then failed on "broken pipe", pipefail ended
    the script before the graph catalogue repair, and the graph was left broken."""
    result = _restore_with_a_reader_that_stops_early(tmp_path)

    assert result.returncode == 0, result.stderr
    commands = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "FLOW_STEWARD_AGE_RESTORE_REPAIR" in commands
    assert "[restore] done" in result.stdout
    assert (tmp_path / "restore.bin").stat().st_size == 2 * 1024 * 1024 + 5  # all of it


def test_a_restore_that_fails_after_the_database_was_replaced_says_not_to_start(
    tmp_path: Path,
) -> None:
    """The failure side: never "run ./install.sh" over a half-restored database."""
    dump_payload = b"pg-dump-payload"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(
        fake_bin, tmp_path / "restore.bin", dump_payload, running_services=("compact-runtime",)
    )
    docker = fake_bin / "docker"
    docker.write_text(
        docker.read_text(encoding="utf-8").replace(
            'if [[ "$args" == *"FLOW_STEWARD_AGE_RESTORE_REPAIR"* ]]; then\n  exit 0',
            'if [[ "$args" == *"FLOW_STEWARD_AGE_RESTORE_REPAIR"* ]]; then\n  exit 3',
        ),
        encoding="utf-8",
    )
    protected_dump = tmp_path / "backups" / "protected.dump.enc"
    protected_dump.parent.mkdir()
    protected_dump.write_bytes(_encrypt_payload(dump_payload, "dedicated-key"))
    env = _script_env(fake_bin, tmp_path, secret="dedicated-key")
    stack_env = tmp_path / "compose.env"
    stack_env.write_text(
        "FS_COMPOSE_FILES=infra/docker/docker-compose.compact.yaml\n", encoding="utf-8"
    )
    env["FS_STACK_ENV_FILE"] = str(stack_env)

    result = subprocess.run(
        [str(DB_RESTORE), str(protected_dump), *_RESTORE_TARGET],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "FAILED after the database was replaced" in result.stderr
    assert "Do not start the runtime" in result.stderr
    assert "run ./install.sh" not in result.stderr
