from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_start_server_enables_uvicorn_reload_when_fs_auto_reload_is_set(tmp_path: Path) -> None:
    uvicorn_stub = tmp_path / "uvicorn"
    uvicorn_stub.write_text(
        "#!/bin/sh\nprintf 'UVICORN_ARGS:'\nprintf ' %s' \"$@\"\nprintf '\\n'\n",
        encoding="utf-8",
    )
    uvicorn_stub.chmod(uvicorn_stub.stat().st_mode | stat.S_IXUSR)
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "dist/.vite").mkdir(parents=True)
    for filename in ("package.json", "package-lock.json", "tsconfig.json", "vite.config.ts"):
        (frontend / filename).write_text("{}", encoding="utf-8")
    (frontend / "dist/.vite/manifest.json").write_text("{}", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "FS_AUTO_RELOAD": "1",
            "FS_SERVE_SSL": "0",
            "FRONTEND_DIR": str(frontend),
            "PORT": "8123",
            "WORKERS": "4",
        }
    )

    result = subprocess.run(
        ["bash", str(ROOT / "core/entrypoints/cli/scripts/start_server.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--reload" in result.stdout
    assert "--reload-dir" in result.stdout
    assert "--reload-exclude" in result.stdout
    assert str(ROOT) in result.stdout
    assert f"{ROOT}/logs/*" in result.stdout
    assert f"{ROOT}/logs/**/*" in result.stdout
    assert "--workers" not in result.stdout


def test_start_server_runs_npm_ci_when_frontend_lock_is_newer(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    manifest = frontend / "dist/.vite/manifest.json"
    node_bin = frontend / "node_modules/.bin"
    node_bin.mkdir(parents=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    (frontend / "src").mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    (node_bin / "vite").write_text("#!/bin/sh\n", encoding="utf-8")
    (node_bin / "vite").chmod((node_bin / "vite").stat().st_mode | stat.S_IXUSR)
    os.utime(manifest, (1, 1))
    os.utime(frontend / "package-lock.json", (2, 2))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "npm").write_text(
        "#!/bin/sh\n"
        'echo npm:$1 >> "$CALL_LOG"\n'
        'if [ "$1" = run ]; then mkdir -p "$FRONTEND_DIR/dist/.vite"; echo \'{}\' > "$FRONTEND_DIR/dist/.vite/manifest.json"; fi\n',
        encoding="utf-8",
    )
    (bin_dir / "uvicorn").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for path in (bin_dir / "npm", bin_dir / "uvicorn"):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    call_log = tmp_path / "calls.log"

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "APP_ROOT": str(tmp_path),
            "FRONTEND_DIR": str(frontend),
            "CALL_LOG": str(call_log),
            "FS_SERVE_SSL": "0",
        }
    )

    subprocess.run(
        ["bash", str(ROOT / "core/entrypoints/cli/scripts/start_server.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert call_log.read_text(encoding="utf-8").splitlines() == ["npm:ci", "npm:run"]


def test_dev_compose_starts_frontend_hmr_stack() -> None:
    payload = yaml.safe_load(
        (ROOT / "infra/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )

    services = payload["services"]

    frontend = services["fs-frontend"]
    assert "env_file" not in frontend
    assert frontend["command"] == ["/usr/local/bin/start_frontend_dev.sh"]
    assert frontend["environment"]["VITE_BACKEND_TARGET"] == "http://fs-app:8099"
    assert frontend["ports"] == ["127.0.0.1:${FRONTEND_HOST_PORT:-15173}:5173"]

    assert "fs-tailwind" not in services

    assert (
        services["mailpit"]["image"]
        == "axllent/mailpit@sha256:757f22b56c1da03570afdb3d259effe5091018008a81bbedc8158cee7e16fdbc"
    )

    frontend_script = (ROOT / "core/entrypoints/cli/scripts/start_frontend_dev.sh").read_text(
        encoding="utf-8"
    )
    start_server_script = (ROOT / "core/entrypoints/cli/scripts/start_server.sh").read_text(
        encoding="utf-8"
    )
    freshness_script = (
        ROOT / "core/entrypoints/cli/scripts/frontend_shell_freshness.sh"
    ).read_text(encoding="utf-8")
    assert "FS_AUTO_RELOAD" in frontend_script
    assert not (ROOT / "core/entrypoints/cli/scripts/start_tailwind_watch.sh").exists()
    assert '--reload-exclude "$APP_ROOT/logs/*"' in start_server_script
    assert '--reload-exclude "$APP_ROOT/logs/**/*"' in start_server_script
    assert 'source "$SCRIPT_DIR/frontend_shell_freshness.sh"' in start_server_script
    assert "! -path '*/test/*'" in freshness_script
    assert "! -name '*.test.tsx'" in freshness_script
    assert "! -name '*.spec.tsx'" in freshness_script

    vite_config = (ROOT / "assets/frontend/vite.config.ts").read_text(encoding="utf-8")
    assert '"/api"' in vite_config
    assert '"/ws"' in vite_config
    assert "usePolling" in vite_config

    dockerfile = (ROOT / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    assert "tailwindcss-linux" not in dockerfile
    assert "assets/tailwind.css" not in dockerfile
