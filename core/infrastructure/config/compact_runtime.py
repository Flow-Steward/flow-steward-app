"""Validated process and port settings for the Compact production runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_MAX_PORT = 65_535
_WORKER_METRICS_BASE_PORT = 9_200


def _decimal_setting(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(environ[name]) if name in environ else str(default)
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a decimal integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class CompactRuntimeSettings:
    """Compact-only environment settings validated before child creation."""

    worker_processes: int
    app_workers: int
    startup_timeout_seconds: int
    restart_limit: int
    restart_window_seconds: int
    restart_backoff_seconds: int
    restart_backoff_max_seconds: int
    shutdown_grace_seconds: int
    kill_reserve_seconds: int
    health_interval_seconds: int
    health_stale_seconds: int
    dependency_timeout_seconds: int

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> CompactRuntimeSettings:
        profile = str(environ.get("FS_DEPLOYMENT_PROFILE") or "scalable")
        worker_processes_explicit = "FS_WORKER_PROCESSES" in environ
        if profile == "scalable" and worker_processes_explicit:
            raise ValueError("FS_WORKER_PROCESSES is Compact-only")
        if profile != "compact":
            raise ValueError("FS_DEPLOYMENT_PROFILE must be compact or scalable")
        settings = cls(
            worker_processes=_decimal_setting(
                environ,
                "FS_WORKER_PROCESSES",
                default=2,
                minimum=1,
                maximum=_MAX_PORT - _WORKER_METRICS_BASE_PORT + 1,
            ),
            app_workers=_decimal_setting(
                (
                    {**environ, "APP_WORKERS": str(environ["WORKERS"])}
                    if "APP_WORKERS" not in environ and "WORKERS" in environ
                    else environ
                ),
                "APP_WORKERS",
                default=1,
                minimum=1,
                maximum=64,
            ),
            startup_timeout_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_STARTUP_TIMEOUT_SECONDS",
                default=300,
                minimum=1,
                maximum=86_400,
            ),
            restart_limit=_decimal_setting(
                environ,
                "FS_COMPACT_RESTART_LIMIT",
                default=3,
                minimum=1,
                maximum=1_000,
            ),
            restart_window_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_RESTART_WINDOW_SECONDS",
                default=300,
                minimum=1,
                maximum=86_400,
            ),
            restart_backoff_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_RESTART_BACKOFF_SECONDS",
                default=2,
                minimum=1,
                maximum=86_400,
            ),
            restart_backoff_max_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_RESTART_BACKOFF_MAX_SECONDS",
                default=30,
                minimum=1,
                maximum=86_400,
            ),
            shutdown_grace_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_SHUTDOWN_GRACE_SECONDS",
                default=180,
                minimum=1,
                maximum=86_400,
            ),
            kill_reserve_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_KILL_RESERVE_SECONDS",
                default=10,
                minimum=1,
                maximum=86_400,
            ),
            health_interval_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_HEALTH_INTERVAL_SECONDS",
                default=2,
                minimum=1,
                maximum=86_400,
            ),
            health_stale_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_HEALTH_STALE_SECONDS",
                default=10,
                minimum=1,
                maximum=86_400,
            ),
            dependency_timeout_seconds=_decimal_setting(
                environ,
                "FS_COMPACT_DEPENDENCY_TIMEOUT_SECONDS",
                default=300,
                minimum=1,
                maximum=86_400,
            ),
        )
        if not (
            settings.kill_reserve_seconds < settings.shutdown_grace_seconds
            and settings.health_interval_seconds < settings.health_stale_seconds
            and settings.restart_backoff_seconds <= settings.restart_backoff_max_seconds
        ):
            raise ValueError("Compact runtime budget relationships are invalid")
        return settings


def validate_compact_only_settings(environ: Mapping[str, str]) -> None:
    """Reject Compact supervisor controls from a Scalable role process."""
    if str(environ.get("FS_DEPLOYMENT_PROFILE") or "scalable") != "scalable":
        return
    compact_only = sorted(name for name in environ if name.startswith("FS_COMPACT_"))
    if "FS_WORKER_PROCESSES" in environ:
        compact_only.append("FS_WORKER_PROCESSES")
    if compact_only:
        raise ValueError(f"{compact_only[0]} is Compact-only")


@dataclass(frozen=True, slots=True)
class CompactPortMap:
    """Complete Compact listener map, validated as one atomic configuration."""

    public_port: int
    app_port: int
    mcp_port: int
    installer_port: int
    health_port: int
    control_metrics_port: int
    worker_metrics_ports: tuple[int, ...]

    @classmethod
    def build(cls, *, public_port: int, worker_processes: int) -> CompactPortMap:
        if not 1 <= public_port <= _MAX_PORT:
            raise ValueError("public port must be between 1 and 65535")
        if worker_processes < 1:
            raise ValueError("worker_processes must be at least 1")
        last_worker_port = _WORKER_METRICS_BASE_PORT + worker_processes - 1
        if last_worker_port > _MAX_PORT:
            raise ValueError("worker metrics port exceeds 65535")
        result = cls(
            public_port=public_port,
            app_port=8_099,
            mcp_port=8_100,
            installer_port=8_101,
            health_port=9_100,
            control_metrics_port=9_102,
            worker_metrics_ports=tuple(range(_WORKER_METRICS_BASE_PORT, last_worker_port + 1)),
        )
        if len(result.all_ports()) != len(set(result.all_ports())):
            raise ValueError("Compact port map contains a collision")
        return result

    def all_ports(self) -> tuple[int, ...]:
        return (
            self.public_port,
            self.app_port,
            self.mcp_port,
            self.installer_port,
            self.health_port,
            self.control_metrics_port,
            *self.worker_metrics_ports,
        )


__all__ = ["CompactPortMap", "CompactRuntimeSettings"]
