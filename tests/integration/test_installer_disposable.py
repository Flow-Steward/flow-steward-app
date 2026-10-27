from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError

import tests.support.disposable_installer as disposable_support
from core.application.installer import InstallationKind, classify_installation
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore
from tests.support.disposable_installer import DisposableInstallerProject

ROOT = Path(__file__).resolve().parents[2]

INSTALL_MATRIX = [
    (environment, renderer, storage)
    for environment in ("production", "development")
    for renderer in ("textual", "plain", "non_interactive")
    for storage in ("bundled", "external_s3")
]

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def disposable_project(
    tmp_path: Path, request: pytest.FixtureRequest
) -> DisposableInstallerProject:
    callspec = getattr(request.node, "callspec", None)
    parameters = callspec.params if callspec is not None else {}
    external_storage = (
        parameters.get("storage") == "external_s3" or "unreachable_external_s3" in request.node.name
    )
    external_tls = (
        parameters.get("environment") == "production"
        or "unreachable_external_s3" in request.node.name
    )
    project = DisposableInstallerProject.create(
        work_root=tmp_path,
        source_root=ROOT,
        external_storage=external_storage,
        external_tls=external_tls,
        external_reachable="unreachable_external_s3" not in request.node.name,
    )
    try:
        yield project
    finally:
        if not project.cleaned:
            project.cleanup()


def _cleanup_and_assert(disposable_project: DisposableInstallerProject) -> None:
    disposable_project.cleanup()
    assert disposable_project.cleanup_proof() == {
        "containers": [],
        "networks": [],
        "volumes": [],
        "images": [],
        "paths": [],
    }


def _create_tls_pair(root: Path, name: str) -> tuple[Path, Path]:
    key = root / f"{name}.key"
    cert = root / f"{name}.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert, key


def test_disposable_port_allocator_avoids_host_ephemeral_source_range() -> None:
    ports = disposable_support._reserve_unique_ports(("first", "second"))

    assert len(set(ports.values())) == 2
    assert all(20_000 <= port < 30_000 for port in ports.values())
    for port in ports.values():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind(("0.0.0.0", port))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(("0.0.0.0", port))


def _start_tls_server(*, cert: Path, key: Path, port: int) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            "openssl",
            "s_server",
            "-quiet",
            "-accept",
            f"127.0.0.1:{port}",
            "-cert",
            str(cert),
            "-key",
            str(key),
            "-www",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return process
        time.sleep(0.05)
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate()
    raise AssertionError("local TLS test server did not start")


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()


def _process_running(pid: str) -> bool:
    state = subprocess.run(
        ["ps", "-p", pid, "-o", "state="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state and not state.startswith("Z"))


def test_clean_clone_materializes_declared_file_transfer_wheels(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        disposable_project._materialize_distribution()
        distribution = disposable_project.distribution_directory
        from tests.support.vendored_wheels import write_vendored_wheel_bundle

        # Built here, so this runs whatever ships — including nothing.
        extension_root = write_vendored_wheel_bundle(distribution / "extensions")
        extension = yaml.safe_load((extension_root / "extension.yaml").read_text(encoding="utf-8"))
        declared_hashes = {
            digest
            for requirement in extension["python_requirements"]
            for digest in requirement["hashes"]
        }
        actual_hashes = {
            f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
            for wheel in (extension_root / "wheels").glob("*.whl")
        }
        manifest = json.loads(disposable_project.manifest_path.read_text(encoding="utf-8"))
        tags = manifest["production_image_tags"]
        compose_ports = {
            "HTTP_PORT": str(disposable_project.ports["http"]),
            "POSTGRES_HOST_PORT": str(disposable_project.ports["postgres"]),
            "RABBITMQ_AMQP_HOST_PORT": str(disposable_project.ports["rabbitmq_amqp"]),
            "RABBITMQ_MANAGEMENT_HOST_PORT": str(disposable_project.ports["rabbitmq_management"]),
            "MAILPIT_SMTP_PORT": str(disposable_project.ports["mailpit_smtp"]),
            "MAILPIT_WEB_PORT": str(disposable_project.ports["mailpit_web"]),
            "MAILPIT_HTTP_PORT": str(disposable_project.ports["mailpit_web"]),
            "FRONTEND_HOST_PORT": str(disposable_project.ports["frontend"]),
            "MCP_HOST_PORT": str(disposable_project.ports["mcp"]),
            "SONAR_PORT": str(disposable_project.ports["sonarqube"]),
            "SEAWEEDFS_S3_HOST_PORT": str(disposable_project.ports["seaweedfs_s3"]),
            "SEAWEEDFS_MASTER_HOST_PORT": str(disposable_project.ports["seaweedfs_master"]),
        }
        launcher_environment = disposable_project._launcher_environment()

        class EmptyInventory:
            def run(self, command, **_kwargs):
                return ProcessOutcome(command=tuple(command), returncode=0)

        gateway = DockerInstallerGateway(
            repo_root=distribution,
            state_store=InstallerStateStore(disposable_project.state_directory),
            process=EmptyInventory(),
            environment={"FS_COMPOSE_PROJECT_NAME": disposable_project.project_name},
        )

        assert actual_hashes == declared_hashes
        assert not (distribution / ".env").exists()
        assert not (distribution / ".env.prod").exists()
        assert classify_installation(gateway.inspect()).kind is InstallationKind.FRESH
        assert {key: launcher_environment[key] for key in compose_ports} == compose_ports
        assert {
            "FS_IMAGE": launcher_environment["FS_IMAGE"],
            "MCP_IMAGE": launcher_environment["MCP_IMAGE"],
            "FS_CLI_IMAGE": launcher_environment["FS_CLI_IMAGE"],
            "POSTGRES_IMAGE": launcher_environment["POSTGRES_IMAGE"],
        } == {
            "FS_IMAGE": tags["app"],
            "MCP_IMAGE": tags["mcp"],
            "FS_CLI_IMAGE": tags["cli"],
            "POSTGRES_IMAGE": tags["postgres"],
        }
        assert launcher_environment["APP_PORT"] == str(disposable_project.ports["public"])
        assert len(set(disposable_project.ports.values())) == len(disposable_project.ports)
        assert "FS_PROJECTS_ROOT" not in launcher_environment
        assert "FS_TEST_PROJECT_ROOT" not in launcher_environment
        assert not (disposable_project.manifest_path.parent / "projects").exists()
        assert not (disposable_project.manifest_path.parent / "test-project").exists()
    finally:
        _cleanup_and_assert(disposable_project)


def test_external_s3_tls_uses_task_owned_ca_inside_extensions(
    tmp_path: Path,
) -> None:
    project = DisposableInstallerProject.create(
        work_root=tmp_path,
        source_root=ROOT,
        external_storage=True,
        external_tls=True,
    )
    try:
        project._materialize_distribution()
        project._record_external_s3_identity(reachable=True, use_tls=True)
        launcher_environment = project._launcher_environment()
        container_ca = launcher_environment["AWS_CA_BUNDLE"]
        assert container_ca.startswith("/app/extensions/")
        host_ca = project.distribution_directory / container_ca.removeprefix("/app/")
        assert host_ca.is_file()
        assert host_ca.stat().st_mode & 0o777 == 0o644
        cert_dir = Path(project.external_s3_identity["certificate_directory"])
        assert (cert_dir / "ca.crt").stat().st_mode & 0o777 == 0o644
        assert (cert_dir / "public.crt").stat().st_mode & 0o777 == 0o644
        assert (cert_dir / "ca.key").stat().st_mode & 0o777 == 0o600
        assert (cert_dir / "private.key").stat().st_mode & 0o777 == 0o600
    finally:
        _cleanup_and_assert(project)


@pytest.mark.parametrize(
    ("storage", "tls_mode", "conditional_services"),
    [
        ("bundled", "local", {"traefik", "seaweedfs"}),
        ("external_s3", "local", {"traefik"}),
        ("bundled", "reverse_proxy", {"seaweedfs"}),
        ("external_s3", "reverse_proxy", set()),
    ],
)
def test_runtime_topology_is_exact_and_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    tls_mode: str,
    conditional_services: set[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"project_name": "owned-project"}), encoding="utf-8")
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    project._last_install = {
        "environment": "production",
        "renderer": "non_interactive",
        "storage": storage,
        "tls_mode": tls_mode,
    }
    expected = {
        "rabbitmq-fs",
        "pg-fs",
        "fs-mcp",
        "fs-app",
        "fs-cli",
        "fs-worker",
        "control-plane",
        *conditional_services,
    }
    rows = [{"service": name, "state": "running", "health": "healthy"} for name in sorted(expected)]
    monkeypatch.setattr(disposable_support, "_project_container_rows", lambda _: rows)

    project._last_install["environment"] = "production"
    assert project.required_services_healthy()
    rows.extend(
        {"service": name, "state": "running", "health": "healthy"}
        for name in (
            "fs-frontend",
            "fs-test-runner",
            "fs-audit",
            "pg-audit",
            "rabbitmq-audit",
        )
    )
    project._last_install["environment"] = "development"
    assert project.required_services_healthy()
    rows.append({"service": "mailpit", "state": "running", "health": "healthy"})
    assert not project.required_services_healthy()
    rows.pop()
    rows[0]["health"] = "unhealthy"
    assert not project.required_services_healthy()


def test_rehydrated_compact_runtime_topology_is_exact_and_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "project_name": "owned-compact-project",
                "state_directory": str(state_directory),
            }
        ),
        encoding="utf-8",
    )
    (state_directory / "stack.toml").write_text(
        'environment = "production"\n'
        'deployment_profile = "compact"\n'
        'file_storage = "external_s3"\n'
        'tls_mode = "reverse_proxy"\n',
        encoding="utf-8",
    )
    rows = [
        {"service": name, "state": "running", "health": "healthy"}
        for name in ("compact-runtime", "fs-cli", "pg-fs", "rabbitmq-fs")
    ]
    monkeypatch.setattr(disposable_support, "_project_container_rows", lambda _: rows)

    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)

    assert project.required_services_healthy()


def test_external_s3_secret_never_appears_in_docker_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "task-owned-secret-must-not-be-an-argument"
    token = "a" * 32
    credentials_path = tmp_path / "credentials.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "owner_token": token,
                "project_name": f"fs-inst-{token}",
                "ports": {"external_s3": 49123},
                "planned_resources": {"images": ["installer:owned", "external-s3:owned"]},
                "external_s3": {
                    "access_key_id": "task-access-key",
                    "credentials_path": str(credentials_path),
                    "certificate_directory": "",
                    "reachable": True,
                },
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(
        manifest_path=manifest_path,
        source_root=ROOT,
        external_s3_secret=secret,
    )
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        calls.append((command, env))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(disposable_support, "_run", fake_run)
    monkeypatch.setattr(
        project,
        "_record_external_s3_identity",
        lambda *, reachable, use_tls: credentials_path,
    )
    monkeypatch.setattr(project, "_record_dependency_image_identity", lambda _reference: None)
    monkeypatch.setattr(project, "_wait_for_external_s3", lambda *, use_tls: None)
    monkeypatch.setattr(project, "_create_external_bucket", lambda *, endpoint, use_tls: None)

    project._start_external_s3(use_tls=False)

    assert "@sha256:" in disposable_support._MINIO_IMAGE
    assert all(secret not in argument for command, _ in calls for argument in command)
    assert all("task-access-key" not in argument for command, _ in calls for argument in command)
    run_command, run_env = next(
        (command, env) for command, env in calls if command[:3] == ["docker", "run", "-d"]
    )
    assert "MINIO_ROOT_PASSWORD" in run_command
    assert run_env is not None
    assert run_env["MINIO_ROOT_USER"] == "task-access-key"
    assert run_env["MINIO_ROOT_PASSWORD"] == secret


def test_textual_launcher_fails_closed_without_review_interaction_marker(
    tmp_path: Path,
) -> None:
    result = disposable_support._run_in_pty(
        [sys.executable, "-c", "print('completed without the installer review screen')"],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=True,
    )

    assert result.returncode != 0
    assert "interaction marker" in result.stderr.lower()


def test_textual_launcher_rejects_plain_fallback_banner_even_with_ansi_review(
    tmp_path: Path,
) -> None:
    script = (
        "import sys,time; "
        "sys.stdout.write('\\x1b[?1049hReview\\n'"
        "+'Terminal is smaller than 80x24; using the plain interactive wizard.\\n'); "
        "sys.stdout.flush(); time.sleep(2)"
    )

    result = disposable_support._run_in_pty(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=True,
    )

    assert result.returncode != 0
    assert "plain fallback" in result.stderr.lower()


def test_textual_launcher_rejects_plain_review_copy_without_terminal_evidence(
    tmp_path: Path,
) -> None:
    script = (
        "import sys,time; "
        "sys.stdout.write('Review pending change:\\n'); "
        "sys.stdout.flush(); time.sleep(2)"
    )

    result = disposable_support._run_in_pty(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=True,
    )

    assert result.returncode != 0
    assert "terminal evidence" in result.stderr.lower()


def test_textual_launcher_accepts_alternate_screen_review_interaction(
    tmp_path: Path,
) -> None:
    script = (
        "import os,sys; "
        "sys.stdout.write('\\x1b[?1049hInstall or safely resume Flow Steward.\\n'); "
        "sys.stdout.flush(); first=os.read(0,64); "
        "sys.stdout.write('Review: Install or safely resume Flow Steward.\\n'); "
        "sys.stdout.flush(); second=os.read(0,64); "
        "raise SystemExit(0 if first == b'\\x1b[Z\\x1b[Z\\n' and second == b'\\n' else 2)"
    )

    result = disposable_support._run_in_pty(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=True,
    )

    assert result.returncode == 0, result.combined_output


def test_textual_launcher_reaches_review_after_external_s3_required_fields(
    tmp_path: Path,
) -> None:
    script = (
        "import os,sys,time; "
        "sys.stdout.write('\\x1b[?1049hInstall or safely resume Flow Steward.\\n'"
        "+'S3 endpoint [Required]\\n'); "
        "sys.stdout.flush(); time.sleep(1.5); "
        "sys.stdout.write('Textual mount complete\\n'); sys.stdout.flush(); "
        "settled=time.monotonic(); first=os.read(0,128); quiet=time.monotonic()-settled; "
        "time.sleep(0.25); "
        "sys.stdout.write('Review: Install or safely resume Flow Steward.\\n'); "
        "sys.stdout.flush(); second=os.read(0,64); "
        "expected=b'\\x1b[Z\\x1b[Z\\n'; "
        "raise SystemExit(0 if quiet >= 0.4 and first == expected and second == b'\\n' else 2)"
    )

    result = disposable_support._run_in_pty(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=True,
        timeout_seconds=5,
    )

    assert result.returncode == 0, result.combined_output


def test_textual_launcher_terminates_child_group_when_parent_interaction_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_pid_path = tmp_path / "pty-child.pid"
    real_select = disposable_support.select.select

    def raise_after_child_started(*args: object, **kwargs: object) -> object:
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise RuntimeError("synthetic parent interaction failure")

    monkeypatch.setattr(disposable_support.select, "select", raise_after_child_started)
    child_pid: int | None = None
    try:
        with pytest.raises(RuntimeError, match="synthetic parent interaction failure"):
            disposable_support._run_in_pty(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,signal,time; "
                        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
                        f"open({str(child_pid_path)!r},'w').write(str(os.getpid())); "
                        "time.sleep(30)"
                    ),
                ],
                cwd=tmp_path,
                env={**os.environ, "TERM": "xterm-256color"},
                textual=True,
                timeout_seconds=5,
            )
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        monkeypatch.setattr(disposable_support.select, "select", real_select)
        if child_pid is None and child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(child_pid, signal.SIGKILL)


def test_plain_launcher_accepts_default_optional_prompts(tmp_path: Path) -> None:
    script = (
        "import os,sys; "
        "sys.stdout.write('Development tools: '); sys.stdout.flush(); "
        "first=os.read(0,64); "
        "sys.stdout.write('Allow insecure S3 HTTP [y/N]: '); sys.stdout.flush(); "
        "second=os.read(0,64); "
        "raise SystemExit(0 if first == b'\\n' and second == b'\\n' else 2)"
    )

    result = disposable_support._run_in_pty(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=False,
        timeout_seconds=5,
    )

    assert result.returncode == 0, result.combined_output


def test_pty_child_sets_terminal_size_before_parent_can_race(tmp_path: Path) -> None:
    child_script = "import os; size=os.get_terminal_size(0); print(size.lines,size.columns)"
    wrapper = (
        "import os,time; "
        "from pathlib import Path; "
        "from tests.support.disposable_installer import _run_in_pty; "
        "os.register_at_fork(after_in_parent=lambda: time.sleep(0.5)); "
        f"result=_run_in_pty([{sys.executable!r},'-c',{child_script!r}],"
        f"cwd=Path({str(tmp_path)!r}),env=dict(os.environ),textual=False); "
        "print(result.stdout,end='')"
    )

    result = subprocess.run(
        [sys.executable, "-c", wrapper],
        cwd=ROOT,
        env={**os.environ, "TERM": "xterm-256color"},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "30 100" in result.stdout


def test_pty_timeout_terminates_the_full_process_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,time; "
        "child=subprocess.Popen(['sleep','30']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    result = disposable_support._run_in_pty(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm-256color"},
        textual=False,
        timeout_seconds=0.5,
    )

    assert result.returncode != 0
    assert "timed out" in result.stderr
    child_pid = child_pid_path.read_text(encoding="utf-8")
    deadline = disposable_support.time.monotonic() + 2
    while disposable_support.time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-p", child_pid, "-o", "state="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            break
        disposable_support.time.sleep(0.05)
    assert not state or state.startswith("Z")


def test_noninteractive_timeout_terminates_the_full_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    child_pid_path = tmp_path / "noninteractive-child.pid"
    launcher = distribution / "install.sh"
    launcher.write_text(
        "#!/bin/sh\n"
        "printf 'launcher-out\\n'\n"
        "printf 'launcher-err\\n' >&2\n"
        "sleep 30 >/dev/null 2>&1 &\n"
        f"printf '%s' \"$!\" > {child_pid_path}\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"distribution_directory": str(distribution)}),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(project, "_launcher_environment", lambda: dict(os.environ))
    monkeypatch.setattr(disposable_support, "_INSTALL_TIMEOUT_SECONDS", 1.5)
    result: disposable_support.InstallerProcessResult | None = None
    timed_out = False
    try:
        try:
            result = project._run_launcher([], renderer="non_interactive")
        except subprocess.TimeoutExpired:
            timed_out = True
        child_pid = child_pid_path.read_text(encoding="utf-8")
        child_survived = _process_running(child_pid)
    finally:
        if child_pid_path.exists():
            child_pid = child_pid_path.read_text(encoding="utf-8")
            if _process_running(child_pid):
                os.kill(int(child_pid), signal.SIGKILL)

    assert not timed_out
    assert result is not None
    assert result.returncode != 0
    assert result.stdout == "launcher-out\n"
    assert result.stderr == "launcher-err\n"
    assert not child_survived


def test_noninteractive_failure_persists_bounded_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    launcher = distribution / "install.sh"
    launcher.write_text("#!/bin/sh\nprintf 'installer failed\\n' >&2\nexit 7\n", encoding="utf-8")
    launcher.chmod(0o700)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "distribution_directory": str(distribution),
                "external_s3": None,
                "project_name": "candidate-project",
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(project, "_launcher_environment", lambda: dict(os.environ))
    monkeypatch.setattr(
        project,
        "_transition_runtime_diagnostic",
        lambda: '[{"service":"fs-app","exit_code":1,"container_logs":"exact cause"}]',
    )

    result = project._run_launcher([], renderer="non_interactive")

    assert result.returncode == 7
    assert "Runtime installation evidence" in result.stderr
    assert "exact cause" in result.stderr
    evidence = json.loads(manifest_path.read_text(encoding="utf-8"))["failure_evidence"]
    assert evidence == {
        "diagnostic": (
            "Launcher stdout tail:\n\n"
            "Launcher stderr tail:\ninstaller failed\n\n"
            "Runtime installation evidence:\n"
            '[{"service":"fs-app","exit_code":1,"container_logs":"exact cause"}]'
        ),
        "operation": "installation",
        "returncode": 7,
    }


def test_manifest_records_baseline_tags_and_refreshes_all_owned_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_inventory = {
        "containers": ["user-container-id"],
        "networks": ["user-network-id"],
        "volumes": ["user-volume-id"],
        "images": ["sha256:user-image"],
    }
    monkeypatch.setattr(disposable_support, "_docker_inventory", lambda: baseline_inventory)
    monkeypatch.setattr(
        disposable_support,
        "_docker_image_tag_mappings",
        lambda: {"user/project:retained": "sha256:user-image"},
    )
    monkeypatch.setattr(disposable_support, "_git_revision", lambda _: "a" * 40)
    project = DisposableInstallerProject.create(work_root=tmp_path, source_root=ROOT)
    manifest = json.loads(project.manifest_path.read_text(encoding="utf-8"))
    token = manifest["owner_token"]
    project_observed = {
        "container": ["late-compose-container"],
        "network": ["late-compose-network"],
        "volume": ["late-compose-volume"],
    }
    owner_observed = {
        "container": ["late-owner-container"],
        "network": ["late-owner-network"],
        "volume": ["late-owner-volume"],
    }
    monkeypatch.setattr(
        disposable_support,
        "_names_with_label",
        lambda kind, project_name: project_observed[kind],
    )
    monkeypatch.setattr(
        disposable_support,
        "_names_with_owner_label",
        lambda kind, owner_token: owner_observed[kind],
    )
    monkeypatch.setattr(disposable_support, "_inspect", lambda kind, identity: None)

    project._refresh_exact_docker_identities()

    refreshed = json.loads(project.manifest_path.read_text(encoding="utf-8"))
    assert refreshed["baseline_image_tags"] == {"user/project:retained": "sha256:user-image"}
    assert refreshed["owner_token"] == token
    for key, kind in (
        ("containers", "container"),
        ("networks", "network"),
        ("volumes", "volume"),
    ):
        assert set(project_observed[kind] + owner_observed[kind]).issubset(
            refreshed["active_resources"][key]
        )


def test_docker_label_query_failure_is_not_treated_as_empty_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, check=False: subprocess.CompletedProcess(
            command, 1, "", "docker daemon unavailable"
        ),
    )

    with pytest.raises(AssertionError, match="Docker label inventory query failed"):
        disposable_support._names_with_label("container", "owned-project")


def test_resource_snapshot_includes_exact_task_image_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project_name": "owned-project",
                "owner_token": "owned-token",
                "production_image_tags": {
                    "app": "owned-app:acceptance",
                    "mcp": "owned-mcp:acceptance",
                },
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(disposable_support, "_names_with_label", lambda kind, project: [])
    monkeypatch.setattr(disposable_support, "_names_with_owner_label", lambda kind, owner: [])
    image_ids = {
        "owned-app:acceptance": "sha256:app",
        "owned-mcp:acceptance": "sha256:mcp",
    }
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, identity: {"Id": image_ids[identity]} if kind == "image" else None,
    )

    snapshot = project._owned_resource_snapshot()

    assert snapshot["images"] == image_ids


def test_repeat_requires_exact_resource_identity_equality(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    before = {
        "containers": {"owned-app": "container-a"},
        "networks": {"owned-network": "network-a"},
        "volumes": {"owned-volume": "volume-a"},
        "images": {"owned-app:acceptance": "sha256:app"},
    }
    project._repeat_evidence = {
        "before": before,
        "after": {
            **before,
            "containers": {
                **before["containers"],
                "unexpected-container": "container-b",
            },
        },
    }

    assert not project.repeat_reused_resources()
    project._repeat_evidence["after"] = before
    assert project.repeat_reused_resources()


def _interruption_project(tmp_path: Path) -> DisposableInstallerProject:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project_name": "fs-inst-round3",
                "planned_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)


def test_interruption_accepts_new_operation_after_clean_runtime_rollback(tmp_path: Path) -> None:
    project = _interruption_project(tmp_path)
    project._interruption_evidence = {
        "operation_before": "interrupted-operation",
        "operation_after": "new-operation",
        "resources_after_rollback": {
            "containers": {},
            "networks": {},
            "volumes": {},
            "images": {"fs-cli-round3:acceptance": "sha256:cached"},
        },
        "resources_after_restart": {
            "containers": {"fs-inst-round3-fs-app-1": "container-app"},
            "networks": {"fs-inst-round3_default": "network-default"},
            "volumes": {"fs-inst-round3_fs-postgres": "volume-postgres"},
            "images": {"fs-cli-round3:acceptance": "sha256:cached"},
        },
    }
    assert project.interruption_restarted_cleanly()

    project._interruption_evidence["operation_after"] = "interrupted-operation"
    assert not project.interruption_restarted_cleanly()


@pytest.mark.parametrize(
    "kind",
    [
        "containers",
        "networks",
        "volumes",
    ],
)
def test_interruption_rejects_runtime_residue_after_rollback(
    tmp_path: Path,
    kind: str,
) -> None:
    project = _interruption_project(tmp_path)
    rollback = {"containers": {}, "networks": {}, "volumes": {}, "images": {}}
    rollback[kind] = {"owned-residue": "resource-id"}
    project._interruption_evidence = {
        "operation_before": "interrupted-operation",
        "operation_after": "new-operation",
        "resources_after_rollback": rollback,
        "resources_after_restart": {
            "containers": {"fs-inst-round3-fs-app-1": "container-app"},
            "networks": {},
            "volumes": {},
            "images": {},
        },
    }

    assert not project.interruption_restarted_cleanly()


def test_interruption_snapshots_clean_rollback_before_new_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    distribution = tmp_path / "distribution"
    state_directory = tmp_path / "state"
    distribution.mkdir()
    state_directory.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "project_name": "fs-inst-round3",
                "owner_token": "round3",
                "distribution_directory": str(distribution),
                "state_directory": str(state_directory),
                "production_image_tags": {"cli": "fs-cli-round3:acceptance"},
                "planned_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                },
            }
        ),
        encoding="utf-8",
    )
    journal_path = state_directory / "install-state.json"
    journal_path.write_text(
        json.dumps({"operation_id": "same-operation", "completed_steps": ["tls"]}),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    events: list[str] = []

    class FakeProcess:
        pid = 4242
        returncode = 130

        def poll(self) -> None:
            return None

        def wait(self, timeout: int | None = None) -> int:
            events.append("stopped")
            journal_path.unlink()
            return self.returncode

    monkeypatch.setattr(project, "_materialize_distribution", lambda: None)
    monkeypatch.setattr(project, "_installer_args", lambda **kwargs: ["install"])
    monkeypatch.setattr(project, "_launcher_environment", dict)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(os, "killpg", lambda pid, sig: events.append("signal"))
    monkeypatch.setattr(project, "_bootstrap_build_process_tree", lambda: "docker build")
    monkeypatch.setattr(project, "_bootstrap_container_id", lambda: "bootstrap-container")
    monkeypatch.setattr(disposable_support, "_inspect", lambda kind, name: None)

    snapshots = iter(
        [
            {
                "containers": {},
                "networks": {},
                "volumes": {},
                "images": {"fs-cli-round3:acceptance": "sha256:cli"},
            },
            {
                "containers": {"fs-inst-round3-pg-fs-1": "container-postgres"},
                "networks": {"fs-inst-round3_default": "network-default"},
                "volumes": {"fs-inst-round3_fs-postgres": "volume-postgres"},
                "images": {
                    "fs-app-round3:acceptance": "sha256:app",
                    "fs-cli-round3:acceptance": "sha256:cli",
                },
            },
        ]
    )

    def snapshot() -> dict[str, dict[str, str]]:
        events.append("snapshot")
        return next(snapshots)

    def restart(*args: object, **kwargs: object) -> disposable_support.InstallerProcessResult:
        events.append("restart")
        journal_path.write_text(
            json.dumps({"operation_id": "new-operation", "completed_steps": ["start", "complete"]}),
            encoding="utf-8",
        )
        return disposable_support.InstallerProcessResult(("install",), 0, "", "")

    monkeypatch.setattr(project, "_owned_resource_snapshot", snapshot)
    monkeypatch.setattr(project, "_run_launcher", restart)
    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)

    project.interrupt_and_restart()

    assert events == ["signal", "stopped", "snapshot", "restart", "snapshot"]


def test_cleanup_verification_records_concurrent_user_image_tag_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "baseline_inventory": {
            "containers": [],
            "networks": [],
            "volumes": [],
            "images": ["sha256:user-image"],
        },
        "baseline_image_tags": {"user/project:retained": "sha256:user-image"},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(
        disposable_support,
        "_docker_inventory",
        lambda: manifest["baseline_inventory"],
    )
    monkeypatch.setattr(
        disposable_support,
        "_docker_image_tag_mappings",
        lambda: {"user/project:retained": "sha256:different-image"},
    )

    assert project._verify_baseline_preserved(manifest) == {
        "inventory_after_cleanup": manifest["baseline_inventory"],
        "missing_baseline_ids": {},
        "changed_baseline_tags": {
            "user/project:retained": {
                "before": "sha256:user-image",
                "after": "sha256:different-image",
            }
        },
    }


def test_cleanup_manifest_records_owned_absence_before_reporting_baseline_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_resources": {
                    "containers": ["owned-container"],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_image_tags": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)
    monkeypatch.setattr(project, "_remove_owned_container", lambda *_args: None)
    monkeypatch.setattr(project, "_verify_resources_absent", lambda _manifest: None)
    monkeypatch.setattr(project, "_remove_unused_dependency_images", lambda _manifest: {})
    monkeypatch.setattr(
        project,
        "_remove_new_anonymous_volumes",
        lambda _manifest: {"post_cleanup_residue": []},
    )
    monkeypatch.setattr(
        project,
        "_verify_baseline_preserved",
        lambda _manifest: (_ for _ in ()).throw(AssertionError("baseline drift")),
    )

    with pytest.raises(AssertionError, match="baseline drift"):
        project.cleanup()

    assert project.cleanup_proof() == {
        "containers": [],
        "networks": [],
        "volumes": [],
        "images": [],
        "paths": [],
    }


def test_cleanup_removes_new_unreferenced_anonymous_volumes_and_records_inventory_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_inventory": {
                    "containers": [],
                    "networks": [],
                    "volumes": ["baseline-anonymous"],
                    "images": [],
                },
                "baseline_image_tags": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    anonymous_inventories = iter(
        [
            ["baseline-anonymous", "task-anonymous"],
            ["baseline-anonymous"],
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)
    monkeypatch.setattr(project, "_remove_unused_dependency_images", lambda _manifest: {})
    monkeypatch.setattr(project, "_verify_resources_absent", lambda _manifest: None)
    monkeypatch.setattr(project, "_verify_baseline_preserved", lambda _manifest: None)
    monkeypatch.setattr(
        disposable_support,
        "_anonymous_volume_names",
        lambda: next(anonymous_inventories),
    )
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, name: (
            {"Labels": {"com.docker.volume.anonymous": ""}}
            if (kind, name) == ("volume", "task-anonymous")
            else None
        ),
    )
    monkeypatch.setattr(
        disposable_support,
        "_volume_container_ids",
        lambda name: [] if name == "task-anonymous" else ["unexpected"],
    )
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, **_kwargs: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "task-anonymous\n", "")
        ),
    )

    project.cleanup()

    assert ["docker", "volume", "rm", "task-anonymous"] in commands
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["anonymous_volume_cleanup"] == {
        "observed_new": ["task-anonymous"],
        "removed": ["task-anonymous"],
        "retained_in_use_or_unproven": [],
        "post_cleanup_residue": [],
    }


def test_cleanup_retains_concurrent_in_use_anonymous_volume_without_task_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "baseline_inventory": {"volumes": []},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(
        disposable_support,
        "_anonymous_volume_names",
        lambda: ["concurrent-anonymous"],
    )
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, name: {"Labels": {"com.docker.volume.anonymous": ""}},
    )
    monkeypatch.setattr(
        disposable_support,
        "_volume_container_ids",
        lambda name: ["unrelated-running-container"],
    )

    evidence = project._remove_new_anonymous_volumes(manifest)

    assert evidence == {
        "observed_new": ["concurrent-anonymous"],
        "removed": [],
        "retained_in_use_or_unproven": ["concurrent-anonymous"],
        "post_cleanup_residue": [],
    }


def test_cleanup_removes_exact_new_build_cache_records_and_records_inventory_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_cache = "a" * 25
    task_cache = "b" * 25
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_inventory": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                },
                "baseline_build_cache": [baseline_cache],
                "baseline_image_tags": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    build_cache_inventories = iter(
        [
            {
                baseline_cache: {"ID": baseline_cache, "Reclaimable": True},
                task_cache: {"ID": task_cache, "Reclaimable": True},
            },
            {baseline_cache: {"ID": baseline_cache, "Reclaimable": True}},
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)
    monkeypatch.setattr(project, "_remove_unused_dependency_images", lambda _manifest: {})
    monkeypatch.setattr(project, "_verify_resources_absent", lambda _manifest: None)
    monkeypatch.setattr(project, "_verify_baseline_preserved", lambda _manifest: None)
    monkeypatch.setattr(
        project,
        "_remove_new_anonymous_volumes",
        lambda _manifest: {"post_cleanup_residue": []},
    )
    monkeypatch.setattr(
        disposable_support,
        "_docker_build_cache_records",
        lambda: next(build_cache_inventories),
    )
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    project.cleanup()

    assert [
        "docker",
        "buildx",
        "prune",
        "--filter",
        f"id={task_cache}",
        "--force",
    ] in commands
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["build_cache_cleanup"] == {
        "observed_new": [task_cache],
        "removed": [task_cache],
        "retained_in_use_or_unproven": [],
        "post_cleanup_residue": [],
    }


def test_cleanup_retains_shared_new_build_cache_without_reporting_disk_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_cache = "a" * 25
    shared_cache = "b" * 25
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_inventory": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                },
                "baseline_build_cache": [baseline_cache],
                "baseline_image_tags": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    shared_record = {
        "ID": shared_cache,
        "Reclaimable": True,
        "Shared": True,
    }
    build_cache_inventories = iter(
        [
            {
                baseline_cache: {"ID": baseline_cache, "Reclaimable": True},
                shared_cache: shared_record,
            },
            {
                baseline_cache: {"ID": baseline_cache, "Reclaimable": True},
                shared_cache: shared_record,
            },
        ]
    )

    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)
    monkeypatch.setattr(project, "_remove_unused_dependency_images", lambda _manifest: {})
    monkeypatch.setattr(project, "_verify_resources_absent", lambda _manifest: None)
    monkeypatch.setattr(project, "_verify_baseline_preserved", lambda _manifest: None)
    monkeypatch.setattr(
        project,
        "_remove_new_anonymous_volumes",
        lambda _manifest: {"post_cleanup_residue": []},
    )
    monkeypatch.setattr(
        disposable_support,
        "_docker_build_cache_records",
        lambda: next(build_cache_inventories),
    )
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    project.cleanup()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["build_cache_cleanup"] == {
        "observed_new": [shared_cache],
        "removed": [],
        "retained_in_use_or_unproven": [shared_cache],
        "post_cleanup_residue": [],
    }


def test_cleanup_prunes_build_cache_children_before_their_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_cache = "z" * 25
    parent_cache = "a" * 25
    child_cache = "b" * 25
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_inventory": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                },
                "baseline_build_cache": [baseline_cache],
                "baseline_image_tags": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    build_cache_inventories = iter(
        [
            {
                baseline_cache: {"ID": baseline_cache, "Reclaimable": True},
                parent_cache: {
                    "ID": parent_cache,
                    "Parents": [],
                    "Reclaimable": True,
                },
                child_cache: {
                    "ID": child_cache,
                    "Parents": [parent_cache],
                    "Reclaimable": True,
                },
            },
            {baseline_cache: {"ID": baseline_cache, "Reclaimable": True}},
        ]
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)
    monkeypatch.setattr(project, "_remove_unused_dependency_images", lambda _manifest: {})
    monkeypatch.setattr(project, "_verify_resources_absent", lambda _manifest: None)
    monkeypatch.setattr(project, "_verify_baseline_preserved", lambda _manifest: None)
    monkeypatch.setattr(
        project,
        "_remove_new_anonymous_volumes",
        lambda _manifest: {"post_cleanup_residue": []},
    )
    monkeypatch.setattr(
        disposable_support,
        "_docker_build_cache_records",
        lambda: next(build_cache_inventories),
    )
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    project.cleanup()

    prune_ids = [
        command[-2].removeprefix("id=")
        for command in commands
        if command[:3] == ["docker", "buildx", "prune"]
    ]
    assert prune_ids == [child_cache, parent_cache]


def test_cleanup_tracks_interrupted_public_installer_container_by_exact_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "a" * 32
    project_name = f"fs-inst-{token}"
    installer_image = f"fs-installer-{token}:acceptance"
    installer_container = "interrupted-installer"
    distribution = tmp_path / "distribution"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "owner_token": token,
                "project_name": project_name,
                "distribution_directory": str(distribution),
                "state_directory": str(distribution / ".flow-steward"),
                "planned_resources": {"images": [installer_image]},
                "production_image_tags": {},
                "observed_image_ids": {},
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    inspected = {
        "Name": f"/{installer_container}",
        "Config": {
            "Image": installer_image,
            "Env": [
                f"FS_COMPOSE_PROJECT_NAME={project_name}",
                f"FS_INSTALLER_STATE_DIR={distribution / '.flow-steward'}",
            ],
            "Labels": {},
        },
        "Mounts": [
            {
                "Source": str(distribution),
                "Destination": str(distribution),
            }
        ],
    }

    monkeypatch.setattr(
        disposable_support,
        "_container_names_for_ancestor",
        lambda image: [installer_container] if image == installer_image else [],
        raising=False,
    )
    monkeypatch.setattr(
        disposable_support,
        "_names_with_owned_labels",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, name: (
            inspected if kind == "container" and name == installer_container else None
        ),
    )

    project._refresh_exact_docker_identities()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["active_resources"]["containers"] == [installer_container]


def test_cleanup_removes_new_unreferenced_dangling_images_and_records_inventory_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_image = "sha256:" + "a" * 64
    parent_image = "sha256:" + "b" * 64
    child_image = "sha256:" + "c" * 64
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_inventory": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [baseline_image],
                },
                "baseline_build_cache": [],
                "baseline_image_tags": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    image_inventories = iter(
        [
            {
                "containers": [],
                "networks": [],
                "volumes": [],
                "images": [baseline_image, parent_image, child_image],
            },
            {
                "containers": [],
                "networks": [],
                "volumes": [],
                "images": [baseline_image],
            },
        ]
    )
    image_details = {
        parent_image: {
            "Id": parent_image,
            "Created": "2026-08-24T10:00:00Z",
            "RepoTags": [],
        },
        child_image: {
            "Id": child_image,
            "Created": "2026-08-24T10:01:00Z",
            "RepoTags": None,
        },
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(project, "_refresh_exact_docker_identities", lambda: None)
    monkeypatch.setattr(project, "_remove_unused_dependency_images", lambda _manifest: {})
    monkeypatch.setattr(project, "_verify_resources_absent", lambda _manifest: None)
    monkeypatch.setattr(project, "_verify_baseline_preserved", lambda _manifest: None)
    monkeypatch.setattr(
        project,
        "_remove_new_anonymous_volumes",
        lambda _manifest: {"post_cleanup_residue": []},
    )
    monkeypatch.setattr(
        project,
        "_remove_new_build_cache_records",
        lambda _manifest: {"post_cleanup_residue": []},
    )
    monkeypatch.setattr(
        disposable_support,
        "_docker_inventory",
        lambda: next(image_inventories),
    )
    monkeypatch.setattr(
        disposable_support,
        "_image_ids_with_container_links",
        set,
    )
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, name: image_details.get(name) if kind == "image" else None,
    )
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    project.cleanup()

    image_removals = [
        command[-1] for command in commands if command[:3] == ["docker", "image", "rm"]
    ]
    assert image_removals == [child_image, parent_image]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["dangling_image_cleanup"] == {
        "observed_new": [parent_image, child_image],
        "removed": [parent_image, child_image],
        "retained_tagged_or_in_use": [],
        "post_cleanup_residue": [],
    }


def test_late_dependency_image_identity_is_recorded_before_resource_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dependency_images": [],
                "observed_dependency_image_ids": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    reference = "registry.example/dependency@sha256:" + ("a" * 64)
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, identity: (
            {"Id": "sha256:" + ("b" * 64)} if (kind, identity) == ("image", reference) else None
        ),
    )

    project._record_dependency_image_identity(reference)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["dependency_images"] == [reference]
    assert manifest["observed_dependency_image_ids"] == {reference: "sha256:" + ("b" * 64)}


def test_profile_capture_retries_and_records_new_compose_dependency_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = "registry.example/scanner@sha256:" + "a" * 64
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project_name": "fs-inst-profile-capture",
                "active_resources": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                    "paths": [],
                },
                "baseline_inventory": {
                    "containers": [],
                    "networks": [],
                    "volumes": [],
                    "images": [],
                },
                "dependency_images": [],
                "observed_dependency_image_ids": {},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(
        project,
        "_active_configuration",
        lambda: {"environment": "development", "file_storage": "bundled"},
    )
    monkeypatch.setattr(
        project,
        "_compose_json_config",
        lambda: {"services": {"sonar-scanner": {"image": image}}},
    )
    monkeypatch.setattr(
        project,
        "_run_active_compose",
        lambda *args: subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"Service": "fs-cli", "ID": "cli-container"}) + "\n",
            "",
        ),
    )
    monkeypatch.setattr(project, "running_services", lambda: {"fs-cli"})
    monkeypatch.setattr(project, "named_volumes", set)
    monkeypatch.setattr(project, "_cli_runtime_mounts", list)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    attempts = 0

    def inspect(kind: str, identity: str) -> dict[str, object] | None:
        nonlocal attempts
        if kind == "container":
            assert identity == "fs-inst-profile-capture-fs-cli-1"
            return {"Id": "c" * 64}
        assert kind == "image"
        assert identity == image
        attempts += 1
        if attempts == 1:
            return None
        return {"Id": "sha256:scanner", "RepoDigests": [image]}

    monkeypatch.setattr(disposable_support, "_inspect", inspect)

    inventory = project.capture_deployment_profile(stage="initial")

    assert attempts == 2
    assert inventory["cli_runtime_container_id"] == "c" * 64
    assert inventory["materialized_services"] == ["sonar-scanner"]
    assert inventory["missing_image_references"] == {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["dependency_images"] == [image]
    assert manifest["observed_dependency_image_ids"] == {image: "sha256:scanner"}


def test_launcher_environment_drops_host_product_configuration_before_acceptance_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SONAR_SCANNER_IMAGE", "sonarsource/sonar-scanner-cli:latest")
    monkeypatch.setenv("PATH", "/acceptance/bin")
    monkeypatch.setattr(disposable_support, "_git_revision", lambda _: "a" * 40)
    monkeypatch.setattr(
        disposable_support,
        "_docker_inventory",
        lambda: {"containers": [], "networks": [], "volumes": [], "images": []},
    )
    monkeypatch.setattr(disposable_support, "_docker_build_cache_records", dict)
    monkeypatch.setattr(disposable_support, "_docker_image_tag_mappings", dict)
    project = DisposableInstallerProject.create(work_root=tmp_path, source_root=ROOT)

    environment = project._launcher_environment()

    assert "SONAR_SCANNER_IMAGE" not in environment
    assert environment["PATH"] == "/acceptance/bin"
    assert environment["FS_COMPOSE_PROJECT_NAME"] == project.project_name
    assert environment["FS_IMAGE"] == project._manifest()["production_image_tags"]["app"]


def test_installed_launcher_environment_does_not_override_persisted_image_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(disposable_support, "_git_revision", lambda _: "a" * 40)
    monkeypatch.setattr(
        disposable_support,
        "_docker_inventory",
        lambda: {"containers": [], "networks": [], "volumes": [], "images": []},
    )
    monkeypatch.setattr(disposable_support, "_docker_image_tag_mappings", dict)
    project = DisposableInstallerProject.create(work_root=tmp_path, source_root=ROOT)

    environment = project._installed_launcher_environment()

    assert {
        "FS_IMAGE",
        "MCP_IMAGE",
        "FS_CLI_IMAGE",
        "POSTGRES_IMAGE",
    }.isdisjoint(environment)
    assert environment["FS_COMPOSE_PROJECT_NAME"] == project.project_name


def test_cleanup_removes_only_unused_dependency_images_absent_from_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_image = "registry.example/new@sha256:" + "b" * 64
    baseline_image = "registry.example/existing:stable"
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "dependency_images": [new_image, baseline_image],
        "observed_dependency_image_ids": {
            new_image: "sha256:new",
            baseline_image: "sha256:baseline",
        },
        "baseline_inventory": {
            "containers": [],
            "networks": [],
            "volumes": [],
            "images": ["sha256:baseline"],
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda kind, identity: {"Id": manifest["observed_dependency_image_ids"][identity]},
    )
    commands: list[list[str]] = []

    def run(
        command: list[str],
        *,
        check: bool = True,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(disposable_support, "_run", run)

    evidence = project._remove_unused_dependency_images(manifest)

    assert commands == [["docker", "image", "rm", new_image]]
    assert evidence == {
        "removed": [new_image],
        "retained_baseline": [baseline_image],
        "retained_changed_or_in_use": [],
    }


def test_lifecycle_credentials_adapter_writes_public_cli_shape_owner_only(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    source = tmp_path / "installer-credentials.json"
    source.write_text(
        json.dumps({"access_key": "candidate-access", "secret_key": "candidate-secret"}),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"distribution_directory": str(distribution)}),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)

    target = project._write_lifecycle_s3_credentials(source)

    assert target == distribution / ".acceptance-external-s3-credentials.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "aws_access_key_id": "candidate-access",
        "aws_secret_access_key": "candidate-secret",
    }
    assert target.stat().st_mode & 0o777 == 0o600


def test_dynamic_external_tls_transition_persists_task_ca_in_active_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(disposable_support, "_git_revision", lambda _: "a" * 40)
    monkeypatch.setattr(
        disposable_support,
        "_docker_inventory",
        lambda: {"containers": [], "networks": [], "volumes": [], "images": []},
    )
    monkeypatch.setattr(disposable_support, "_docker_image_tag_mappings", dict)
    project = DisposableInstallerProject.create(
        work_root=tmp_path,
        source_root=ROOT,
        external_storage=True,
        external_tls=True,
    )
    project.distribution_directory.mkdir()
    environment_path = project.distribution_directory / ".env"
    environment_path.write_text(
        "AWS_CA_BUNDLE=/stale/ca.crt\nFS_STACK_ENVIRONMENT=development\n",
        encoding="utf-8",
    )
    project._external_host_ca_path().parent.mkdir(parents=True)
    project._external_host_ca_path().write_text("test ca\n", encoding="utf-8")
    monkeypatch.setattr(
        project,
        "_active_configuration",
        lambda: {"environment": "development", "file_storage": "bundled"},
    )

    project._persist_external_ca_bundle_for_lifecycle()

    text = environment_path.read_text(encoding="utf-8")
    assert text.count("AWS_CA_BUNDLE=") == 1
    assert f"AWS_CA_BUNDLE={project._external_container_ca_path()}" in text
    assert "/stale/ca.crt" not in text
    assert environment_path.stat().st_mode & 0o777 == 0o600


def test_transition_failure_evidence_is_bounded_redacted_and_persisted(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "external_s3": {"access_key_id": "acceptance-access"},
                "owner_token": "owner-token",
                "state_directory": str(tmp_path / ".flow-steward"),
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(
        manifest_path=manifest_path,
        source_root=ROOT,
        external_s3_secret="acceptance-secret",
    )
    operations = tmp_path / ".flow-steward" / "operations"
    operations.mkdir(parents=True)
    before = {
        "operation_id": "operation-1",
        "stage": "before",
        "database_reachable": True,
        "fields": {
            "environment": "development",
            "durable_volumes": ["volume-before"],
            "schema_fingerprint": "unknown",
        },
    }
    after = {
        **before,
        "stage": "after",
        "fields": {
            **before["fields"],
            "environment": "production",
            "durable_volumes": ["volume-after"],
        },
    }
    (operations / "operation-1.before-preservation.json").write_text(
        json.dumps(before), encoding="utf-8"
    )
    (operations / "operation-1.after-preservation.json").write_text(
        json.dumps(after), encoding="utf-8"
    )
    diagnostic = (
        "prefix-which-must-be-truncated\n"
        + ("x" * 9_000)
        + "\nacceptance-access acceptance-secret exact-transition-cause"
    )

    project._record_transition_failure_evidence(
        target="production",
        storage="external_s3",
        returncode=1,
        diagnostic=diagnostic,
    )

    evidence = json.loads(manifest_path.read_text(encoding="utf-8"))["failure_evidence"]
    assert evidence["operation"] == "environment switch"
    assert evidence["target"] == "production"
    assert evidence["storage"] == "external_s3"
    assert evidence["returncode"] == 1
    assert len(evidence["diagnostic"]) <= 8_192
    assert "prefix-which-must-be-truncated" not in evidence["diagnostic"]
    assert "exact-transition-cause" in evidence["diagnostic"]
    assert "acceptance-access" not in evidence["diagnostic"]
    assert "acceptance-secret" not in evidence["diagnostic"]
    assert evidence["diagnostic"].count("[REDACTED]") == 2
    assert evidence["preservation_comparison"] == {
        "operation_id": "operation-1",
        "database_reachable": {"before": True, "after": True},
        "unknown_fields": {"before": ["schema_fingerprint"], "after": ["schema_fingerprint"]},
        "changed_fields": [
            {
                "field": "durable_volumes",
                "before": ["volume-before"],
                "after": ["volume-after"],
            },
            {
                "field": "environment",
                "before": "development",
                "after": "production",
            },
        ],
    }


def test_installed_cli_failure_evidence_preserves_comparison(tmp_path: Path) -> None:
    project = DisposableInstallerProject.create(work_root=tmp_path, source_root=ROOT)
    project.distribution_directory.mkdir()
    operations = project.state_directory / "operations"
    operations.mkdir(parents=True)
    before = {
        "operation_id": "release-upgrade-1",
        "stage": "before",
        "database_reachable": True,
        "fields": {"release": "0.1.0"},
    }
    after = {
        **before,
        "stage": "after",
        "fields": {"release": "candidate"},
    }
    (operations / "release-upgrade-1.before-preservation.json").write_text(
        json.dumps(before), encoding="utf-8"
    )
    (operations / "release-upgrade-1.after-preservation.json").write_text(
        json.dumps(after), encoding="utf-8"
    )

    project._record_installed_cli_failure_evidence(
        command_id="release.upgrade",
        returncode=9,
        diagnostic="release preservation failed",
    )

    evidence = json.loads(project.manifest_path.read_text(encoding="utf-8"))["failure_evidence"]
    assert evidence["operation"] == "installed_cli"
    assert evidence["command_id"] == "release.upgrade"
    assert evidence["returncode"] == 9
    assert evidence["preservation_comparison"] == {
        "operation_id": "release-upgrade-1",
        "database_reachable": {"before": True, "after": True},
        "unknown_fields": {"before": [], "after": []},
        "changed_fields": [{"field": "release", "before": "0.1.0", "after": "candidate"}],
    }


def test_transition_runtime_diagnostic_captures_redacted_app_logs_while_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project_name": "candidate-project",
                "external_s3": {"access_key_id": "acceptance-access"},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(
        manifest_path=manifest_path,
        source_root=ROOT,
        external_s3_secret="acceptance-secret",
    )
    monkeypatch.setattr(
        disposable_support,
        "_container_names_for_project",
        lambda _project: ["candidate-app"],
    )
    monkeypatch.setattr(
        disposable_support,
        "_inspect",
        lambda _kind, _name: {
            "Config": {"Labels": {"com.docker.compose.service": "fs-app"}},
            "State": {"Status": "running", "ExitCode": 0},
        },
    )
    monkeypatch.setattr(
        disposable_support,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            "acceptance-access acceptance-secret exact-container-cause\n",
            "",
        ),
    )

    diagnostic = project._transition_runtime_diagnostic()

    record = json.loads(diagnostic)[0]
    assert record["service"] == "fs-app"
    assert record["exit_code"] == 0
    assert "exact-container-cause" in record["container_logs"]
    assert "acceptance-access" not in diagnostic
    assert "acceptance-secret" not in diagnostic


def test_storage_unavailable_proof_accepts_only_missing_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    sentinel = {"storage": {"key": "local-only"}}
    missing = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
        "GetObject",
    )
    monkeypatch.setattr(
        project,
        "_storage_sentinel",
        lambda **_kwargs: (_ for _ in ()).throw(missing),
    )

    project.assert_storage_transition_sentinel_unavailable(sentinel)

    monkeypatch.setattr(project, "_storage_sentinel", lambda **_kwargs: b"unexpected-copy")
    with pytest.raises(AssertionError, match="unexpectedly available"):
        project.assert_storage_transition_sentinel_unavailable(sentinel)


def test_generated_tls_public_route_rejects_untrusted_served_certificate(
    tmp_path: Path,
) -> None:
    trusted_cert, _trusted_key = _create_tls_pair(tmp_path, "trusted")
    served_cert, served_key = _create_tls_pair(tmp_path, "served")
    distribution = tmp_path / "distribution"
    (distribution / ".ssl").mkdir(parents=True)
    (distribution / ".ssl/cert.pem").write_bytes(trusted_cert.read_bytes())
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = int(reserved.getsockname()[1])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "distribution_directory": str(distribution),
                "ports": {"public": port, "reverse_proxy_backend": port},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    server = _start_tls_server(cert=served_cert, key=served_key, port=port)
    try:
        assert not project.public_route_healthy(tls_mode="local")
    finally:
        _stop_process_group(server)


def test_provided_tls_public_route_requires_exact_provided_certificate(
    tmp_path: Path,
) -> None:
    served_cert, served_key = _create_tls_pair(tmp_path, "served")
    other_cert, _other_key = _create_tls_pair(tmp_path, "other")
    distribution = tmp_path / "distribution"
    (distribution / ".ssl").mkdir(parents=True)
    (distribution / ".acceptance-tls").mkdir()
    (distribution / ".ssl/cert.pem").write_bytes(served_cert.read_bytes())
    provided_path = distribution / ".acceptance-tls/cert.pem"
    provided_path.write_bytes(other_cert.read_bytes())
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = int(reserved.getsockname()[1])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "distribution_directory": str(distribution),
                "ports": {"public": port, "reverse_proxy_backend": port},
            }
        ),
        encoding="utf-8",
    )
    project = DisposableInstallerProject(manifest_path=manifest_path, source_root=ROOT)
    server = _start_tls_server(cert=served_cert, key=served_key, port=port)
    try:
        assert not project.public_route_healthy(tls_mode="provided")
        provided_path.write_bytes(served_cert.read_bytes())
        assert project.public_route_healthy(tls_mode="provided")
    finally:
        _stop_process_group(server)


@pytest.mark.parametrize(("environment", "renderer", "storage"), INSTALL_MATRIX)
def test_disposable_install_reaches_storage_round_trip(
    environment: str,
    renderer: str,
    storage: str,
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        result = disposable_project.install(
            environment=environment,
            renderer=renderer,
            storage=storage,
        )

        assert result.returncode == 0, result.combined_output
        assert disposable_project.required_services_healthy()
        assert disposable_project.storage_round_trip() == b"installer-proof"
        assert not disposable_project.output_contains_external_credentials()
        if storage == "bundled":
            assert disposable_project.external_s3_identity is None
    finally:
        _cleanup_and_assert(disposable_project)


def test_interrupted_install_rolls_back_then_restarts_from_beginning(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        interrupted, restarted = disposable_project.interrupt_and_restart()

        assert interrupted.returncode == 10, interrupted.combined_output
        assert "installation_interrupted" in interrupted.combined_output
        assert restarted.returncode == 0, restarted.combined_output
        assert disposable_project.journal_status() == "completed"
        assert disposable_project.interruption_restarted_cleanly()
        assert disposable_project.required_services_healthy()
    finally:
        _cleanup_and_assert(disposable_project)


def test_repeat_after_completion_reports_already_installed(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        first = disposable_project.install(
            environment="development",
            renderer="non_interactive",
            storage="bundled",
        )
        assert first.returncode == 0, first.combined_output

        repeated = disposable_project.repeat_install()

        assert repeated.returncode == 0, repeated.combined_output
        assert "already installed" in repeated.combined_output.lower()
        assert disposable_project.repeat_reused_resources()
        assert disposable_project.required_services_healthy()
    finally:
        _cleanup_and_assert(disposable_project)


def test_unreachable_external_s3_refuses_with_specific_redacted_error(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        result = disposable_project.install_with_unreachable_s3()

        assert result.returncode == 8, result.combined_output
        assert "storage_verification_failed" in result.combined_output
        assert disposable_project.external_s3_secret not in result.combined_output
        assert not disposable_project.has_bundled_storage_resources()
    finally:
        _cleanup_and_assert(disposable_project)


def test_occupied_public_port_refuses_before_compose_mutation(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        result = disposable_project.install_with_occupied_public_port()

        assert result.returncode == 4, result.combined_output
        assert "port_conflict" in result.combined_output
        assert not disposable_project.has_compose_resources()
    finally:
        _cleanup_and_assert(disposable_project)


def test_insufficient_disk_refuses_before_compose_mutation(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        result = disposable_project.install_on_low_capacity_filesystem()

        assert result.returncode == 4, result.combined_output
        assert "insufficient_disk_space" in result.combined_output
        assert not disposable_project.has_compose_resources()
    finally:
        _cleanup_and_assert(disposable_project)


@pytest.mark.parametrize("tls_mode", ("local", "provided", "reverse_proxy"))
def test_tls_mode_reaches_healthy_public_route(
    tls_mode: str,
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        result = disposable_project.install(
            environment="development",
            renderer="non_interactive",
            storage="bundled",
            tls_mode=tls_mode,
        )

        assert result.returncode == 0, result.combined_output
        assert disposable_project.public_route_healthy(tls_mode=tls_mode)
    finally:
        _cleanup_and_assert(disposable_project)


def test_ambiguous_owned_state_refuses_without_adoption(
    disposable_project: DisposableInstallerProject,
) -> None:
    try:
        result = disposable_project.install_with_ambiguous_volume()

        assert result.returncode == 9, result.combined_output
        assert "ambiguous_installation_state" in result.combined_output
        assert "preserved" in result.combined_output.lower()
        assert not disposable_project.has_compose_containers()
    finally:
        _cleanup_and_assert(disposable_project)
