"""Shared runtime environment helpers for test and testing-tooling flows."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

VALID_LANES = ("ui", "backend")
VALID_ROLES = ("admin", "operator", "viewer")

DEFAULT_USERNAMES = {
    "admin": "e2e_admin@example.com",
    "operator": "e2e_operator@example.com",
    "viewer": "e2e_viewer@example.com",
}
DEFAULT_PASSWORDS = {
    "admin": "e2e_admin_password_123!",
    "operator": "e2e_operator_password_123!",
    "viewer": "e2e_viewer_password_123!",
}
DEFAULT_DISPLAY_NAMES = {
    "admin": "E2E Admin",
    "operator": "E2E Operator",
    "viewer": "E2E Viewer",
}
DEFAULT_PROJECT_NAMES = {
    "ui": "Test Project UI",
    "backend": "Test Project Backend",
}
LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}
INTERNAL_DOCKER_HOST = "fs-app"
REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class ResolvedTestEnv:
    lane: str
    role: str
    project_id: str
    project_name: str
    username: str
    password: str
    display_name: str


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_truthy_flag(value: object) -> bool:
    return _clean(value).lower() in {"1", "true", "yes", "on"}


def _first(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = _clean(env.get(name))
        if value:
            return value
    return ""


def _resolve_lane(lane: str | None) -> str:
    candidate = _clean(lane).lower()
    return candidate if candidate in VALID_LANES else "backend"


def _read_dotenv(path: Path | None = None) -> dict[str, str]:
    dotenv_path = path or (REPO_ROOT / ".env")
    if not dotenv_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _merge_env(env: Mapping[str, str], include_dotenv: bool) -> Mapping[str, str]:
    if not include_dotenv:
        return env
    merged = _read_dotenv()
    merged.update({key: _clean(value) for key, value in env.items()})
    return merged


def _normalize_local_runtime_url(raw_url: str, env: Mapping[str, str]) -> str:
    value = _clean(raw_url)
    if not value:
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value

    host = _clean(parsed.hostname).lower()
    if (
        parsed.scheme == "https"
        and host in LOCALHOST_HOSTS
        and not _is_truthy_flag(env.get("FS_SERVE_SSL"))
    ):
        return urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    if (
        parsed.scheme == "wss"
        and host in LOCALHOST_HOSTS
        and not _is_truthy_flag(env.get("FS_SERVE_SSL"))
    ):
        return urlunsplit(("ws", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def _url_host(raw_url: str) -> str:
    value = _clean(raw_url)
    if not value:
        return ""
    try:
        return _clean(urlsplit(value).hostname).lower()
    except Exception:
        return ""


def _running_in_container(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else _runtime_env
    override = _first(source, "TEST_RUNNER_IN_DOCKER")
    if override:
        return _is_truthy_flag(override)

    if Path("/.dockerenv").exists():
        return True

    cgroup_path = Path("/proc/1/cgroup")
    try:
        cgroup_text = cgroup_path.read_text(encoding="utf-8")
    except OSError:
        return False
    lowered = cgroup_text.lower()
    return any(marker in lowered for marker in ("docker", "containerd", "podman", "kubepods"))


def _internal_compose_runtime_url(raw_url: str, env: Mapping[str, str]) -> str:
    value = _clean(raw_url)
    if not value or not _running_in_container(env):
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value

    host = _clean(parsed.hostname).lower()
    if host not in LOCALHOST_HOSTS:
        return value

    scheme = _clean(parsed.scheme).lower()
    if scheme not in {"http", "https", "ws", "wss"}:
        return value

    if scheme in {"https", "wss"} and not _is_truthy_flag(env.get("FS_SERVE_SSL")):
        scheme = "http" if scheme == "https" else "ws"

    port = parsed.port or 8099
    return urlunsplit(
        (scheme, f"{INTERNAL_DOCKER_HOST}:{port}", parsed.path, parsed.query, parsed.fragment)
    )


def _external_host_runtime_url(raw_url: str, env: Mapping[str, str]) -> str:
    value = _clean(raw_url)
    if not value or _running_in_container(env):
        return value
    try:
        parsed = urlsplit(value)
    except Exception:
        return value

    host = _clean(parsed.hostname).lower()
    if host != INTERNAL_DOCKER_HOST:
        return value

    scheme = _clean(parsed.scheme).lower() or "http"
    if scheme in {"https", "wss"} and not _is_truthy_flag(env.get("FS_SERVE_SSL")):
        scheme = "http" if scheme == "https" else "ws"

    port = parsed.port or 8099
    return urlunsplit((scheme, f"localhost:{port}", parsed.path, parsed.query, parsed.fragment))


def _resolve_role(env: Mapping[str, str], lane: str, override: str | None) -> str:
    if _clean(override).lower() in VALID_ROLES:
        return _clean(override).lower()
    candidate = _first(
        env,
        f"E2E_{lane.upper()}_ROLE",
        "NIGHTFIRE_AUTH_ROLE",
    ).lower()
    return candidate if candidate in VALID_ROLES else "admin"


def _resolve_project_id(env: Mapping[str, str], lane: str) -> str:
    if lane == "ui":
        return _first(env, "TEST_PROJECT_UI_ID")
    return _first(env, "TEST_PROJECT_BACKEND_ID")


def _resolve_project_name(env: Mapping[str, str], lane: str) -> str:
    if lane == "ui":
        return _first(env, "TEST_PROJECT_UI_NAME") or DEFAULT_PROJECT_NAMES["ui"]
    return _first(env, "TEST_PROJECT_BACKEND_NAME") or DEFAULT_PROJECT_NAMES["backend"]


def _resolve_username(env: Mapping[str, str], role: str) -> str:
    return _first(env, f"E2E_{role.upper()}_USERNAME") or DEFAULT_USERNAMES[role]


def _resolve_password(env: Mapping[str, str], role: str) -> str:
    return _first(env, f"E2E_{role.upper()}_PASSWORD") or DEFAULT_PASSWORDS[role]


def _resolve_display_name(env: Mapping[str, str], role: str, username: str) -> str:
    return (
        _first(env, f"E2E_{role.upper()}_DISPLAY_NAME") or DEFAULT_DISPLAY_NAMES[role] or username
    )


def resolve_base_url(
    env: Mapping[str, str] | None = None,
    *,
    default: str = "http://localhost:8099",
    include_dotenv: bool = False,
) -> str:
    raw_env = env if env is not None else _runtime_env
    source = _merge_env(raw_env, include_dotenv)

    explicit_env_base = _first(raw_env, "APP_BASE_URL")
    explicit_runner_base = _first(raw_env, "TEST_RUNNER_BASE_URL")

    if explicit_env_base:
        if (
            explicit_runner_base
            and _running_in_container(raw_env)
            and _url_host(explicit_env_base) in LOCALHOST_HOSTS
        ):
            return explicit_runner_base
        # Explicit APP_BASE_URL always wins. Tests and operators rely on this
        # value being stable and not silently rewritten for container internals.
        return explicit_env_base

    if explicit_runner_base:
        return _external_host_runtime_url(explicit_runner_base, raw_env)

    runner_base = _first(source, "TEST_RUNNER_BASE_URL")
    if runner_base:
        return _external_host_runtime_url(runner_base, source)

    explicit = _first(source, "APP_BASE_URL")
    if explicit:
        # Respect explicit user/operator intent for public runtime URL.
        # FS_SERVE_SSL reflects app-server transport (often HTTP behind Traefik),
        # and should not silently rewrite an explicitly configured external base URL.
        if explicit_env_base:
            return explicit
        normalized_explicit = _normalize_local_runtime_url(explicit, source)
        return _internal_compose_runtime_url(normalized_explicit, source)
    normalized_default = _normalize_local_runtime_url(default, source)
    return _internal_compose_runtime_url(normalized_default, source)


def resolve_ws_url(
    env: Mapping[str, str] | None = None,
    *,
    default: str = "ws://localhost:8099",
    include_dotenv: bool = False,
) -> str:
    source = _merge_env(env if env is not None else _runtime_env, include_dotenv)
    raw = _first(source, "WS_URL")
    if raw:
        return raw

    base_url = resolve_base_url(
        source,
        default=default.replace("wss://", "https://", 1).replace("ws://", "http://", 1),
        include_dotenv=False,
    )
    try:
        parsed = urlsplit(base_url)
    except Exception:
        return default

    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def resolve_test_env(
    env: Mapping[str, str] | None = None,
    *,
    lane: str = "backend",
    role: str | None = None,
    include_dotenv: bool = False,
) -> ResolvedTestEnv:
    source = _merge_env(env if env is not None else _runtime_env, include_dotenv)
    resolved_lane = _resolve_lane(lane)
    resolved_role = _resolve_role(source, resolved_lane, role)
    username = _resolve_username(source, resolved_role)
    return ResolvedTestEnv(
        lane=resolved_lane,
        role=resolved_role,
        project_id=_resolve_project_id(source, resolved_lane),
        project_name=_resolve_project_name(source, resolved_lane),
        username=username,
        password=_resolve_password(source, resolved_role),
        display_name=_resolve_display_name(source, resolved_role, username),
    )


def apply_test_lane_defaults(
    target_env: MutableMapping[str, str] | None = None,
    *,
    lane: str = "backend",
    role: str | None = None,
    include_dotenv: bool = False,
) -> ResolvedTestEnv:
    env = target_env if target_env is not None else _runtime_env
    resolved = resolve_test_env(env, lane=lane, role=role, include_dotenv=include_dotenv)
    normalized_base_url = resolve_base_url(env, include_dotenv=include_dotenv)
    if normalized_base_url:
        env["APP_BASE_URL"] = normalized_base_url
    normalized_ws_url = resolve_ws_url(env, include_dotenv=include_dotenv)
    if normalized_ws_url:
        env["WS_URL"] = normalized_ws_url
    return resolved
