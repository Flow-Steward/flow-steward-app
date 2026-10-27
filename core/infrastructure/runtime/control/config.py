"""Shared configuration for control-plane loops.

Reads from the same ``FS_*`` environment variables that the app runtime uses,
so the control-plane process and the app process share a single source of
truth when both are deployed from the same image.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _env_int(name: str, default: str) -> int:
    """Read a numeric setting, treating an empty value as one that was never set.

    AC17-R105 follow-up. Compose substitutes every key it names, so a variable an
    operator left alone arrives as an empty string rather than not arriving, and
    `int(os.environ.get(name, "30"))` returns the default only for a missing key. An
    empty one raised ValueError here, the control process crash-looped, and with the
    control plane down no role in the runtime came up at all.

    Fixing it where the value is read keeps the default in one place. Declaring it in
    the Compose overlay instead would have worked too, and would have put a second
    copy of every number beside the one here -- copies that already disagree elsewhere
    in this repository.
    """
    raw = _runtime_env.get(name)
    value = str(raw if raw is not None else "").strip()
    return int(value or default)


def _env_flag(name: str, default: bool) -> bool:
    raw = _runtime_env.get(name)
    if raw is None:
        return bool(default)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


@dataclass(frozen=True)
class ControlConfig:
    """Configuration for the watchdog and scheduled-jobs control loops."""

    watchdog_enabled: bool
    watchdog_interval: float
    watchdog_auto_apply: bool
    watchdog_stuck_seconds: int
    watchdog_orphan_grace_seconds: int
    watchdog_pickup_grace_seconds: int
    watchdog_max_journal_entries: int

    scheduled_jobs_enabled: bool
    scheduled_jobs_interval: float
    scheduled_jobs_batch_limit: int

    dashboard_rollup_enabled: bool
    dashboard_rollup_interval: float
    dashboard_rollup_backfill_days: int
    dashboard_rollup_lookback_days: int

    security_posture_enabled: bool
    security_posture_interval: float

    workflow_knowledge_sync_enabled: bool
    workflow_knowledge_sync_interval: float
    workflow_knowledge_sync_changelog_limit: int

    external_effect_maintenance_enabled: bool = True
    external_effect_maintenance_interval: float = 300.0
    artifact_cleanup_reconciliation_budget: int = 16
    # Retention (trash, logs, jobs, revisions) runs here so every profile gets it
    # without a host cron: Compact, Scalable and a platform with no host at all.
    retention_enabled: bool = True
    retention_interval: float = 86400.0

    @classmethod
    def from_env(cls) -> ControlConfig:
        return cls(
            watchdog_enabled=_env_flag("FS_WATCHDOG_ENABLED", True),
            watchdog_interval=max(5, _env_int("FS_WATCHDOG_INTERVAL_SECONDS", "30")),
            watchdog_auto_apply=_env_flag("FS_WATCHDOG_AUTO_APPLY", True),
            watchdog_stuck_seconds=_env_int("FS_WATCHDOG_STUCK_SECONDS", "180"),
            watchdog_orphan_grace_seconds=_env_int("FS_WATCHDOG_ORPHAN_GRACE_SECONDS", "120"),
            watchdog_pickup_grace_seconds=max(
                5, _env_int("FS_WATCHDOG_PICKUP_GRACE_SECONDS", "120")
            ),
            watchdog_max_journal_entries=_env_int("FS_WATCHDOG_MAX_JOURNAL_ENTRIES", "10000"),
            scheduled_jobs_enabled=_env_flag("FS_SCHEDULED_JOBS_ENABLED", True),
            scheduled_jobs_interval=max(10, _env_int("FS_SCHEDULED_JOBS_INTERVAL_SECONDS", "10")),
            scheduled_jobs_batch_limit=max(1, _env_int("FS_SCHEDULED_JOBS_BATCH_LIMIT", "100")),
            dashboard_rollup_enabled=_env_flag("FS_DASHBOARD_ROLLUP_ENABLED", True),
            dashboard_rollup_interval=max(
                30, _env_int("FS_DASHBOARD_ROLLUP_INTERVAL_SECONDS", "300")
            ),
            dashboard_rollup_backfill_days=max(
                1, _env_int("FS_DASHBOARD_ROLLUP_BACKFILL_DAYS", "7")
            ),
            dashboard_rollup_lookback_days=max(
                1, _env_int("FS_DASHBOARD_ROLLUP_LOOKBACK_DAYS", "30")
            ),
            security_posture_enabled=_env_flag("FS_SECURITY_POSTURE_ENABLED", True),
            security_posture_interval=max(
                60, _env_int("FS_SECURITY_POSTURE_INTERVAL_SECONDS", "300")
            ),
            workflow_knowledge_sync_enabled=_env_flag("FS_WORKFLOW_KNOWLEDGE_SYNC_ENABLED", True),
            workflow_knowledge_sync_interval=max(
                30,
                _env_int("FS_WORKFLOW_KNOWLEDGE_SYNC_INTERVAL_SECONDS", "300"),
            ),
            workflow_knowledge_sync_changelog_limit=max(
                1,
                _env_int("FS_WORKFLOW_KNOWLEDGE_SYNC_CHANGELOG_LIMIT", "500"),
            ),
            external_effect_maintenance_enabled=_env_flag(
                "FS_EXTERNAL_EFFECT_MAINTENANCE_ENABLED", True
            ),
            external_effect_maintenance_interval=max(
                30,
                _env_int("FS_EXTERNAL_EFFECT_MAINTENANCE_INTERVAL_SECONDS", "300"),
            ),
            artifact_cleanup_reconciliation_budget=max(
                1,
                min(
                    128,
                    _env_int("FS_ARTIFACT_CLEANUP_RECONCILIATION_BUDGET", "16"),
                ),
            ),
            retention_enabled=_env_flag("FS_RETENTION_ENABLED", True),
            retention_interval=max(3600, _env_int("FS_RETENTION_INTERVAL_SECONDS", "86400")),
        )
