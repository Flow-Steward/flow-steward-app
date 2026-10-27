"""SEC-013: Security Event Logger — centralized persistence of scan events.

Writes to ``security_events`` and ``security_findings`` tables.
All writes are non-fatal: observability failure never interrupts operations.
Raw secrets/PII are NEVER stored — only redacted previews.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql
from core.infrastructure.security.models import ScanFinding, ScanReport

logger = logging.getLogger(__name__)

_instance: SecurityEventLogger | None = None


def get_security_logger() -> SecurityEventLogger | None:
    """Return a singleton SecurityEventLogger (lazy-initialized)."""
    global _instance
    if _instance is not None:
        return _instance

    dsn = (
        _runtime_env.get("FS_OBSERVABILITY_DSN")
        or _runtime_env.get("FS_DB_DSN")
        or _runtime_env.get("DATABASE_URL")
        or _runtime_env.get("POSTGRES_DSN")
    )
    if not dsn:
        return None

    try:
        _instance = SecurityEventLogger(dsn)
        return _instance
    except Exception:
        logger.debug("SecurityEventLogger init failed — will retry later")
        return None


class SecurityEventLogger:
    """Writes security scan events and findings to PostgreSQL."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None

    def _get_pool(self):
        if self._pool is not None:
            return self._pool
        try:
            from core.infrastructure.db.db_pool import get_threaded_pool, pool_bounds

            minconn, maxconn = pool_bounds()
            self._pool = get_threaded_pool(self._dsn, minconn=minconn, maxconn=maxconn)
            return self._pool
        except Exception:
            return None

    def _conn(self):
        pool = self._get_pool()
        if pool is None:
            return None
        try:
            return pool.getconn()
        except Exception:
            return None

    def _put_conn(self, conn):
        pool = self._get_pool()
        if pool and conn:
            with suppress(Exception):
                pool.putconn(conn)

    def _resolve_write_account_id(
        self,
        cur,
        *,
        account_id: str | None,
        project_id: Any | None,
    ) -> str | None:
        candidate = str(account_id or "").strip()
        if candidate and candidate != "default":
            return candidate
        if project_id is None:
            return None
        cur.execute(
            """
            SELECT account_id
            FROM projects
            WHERE id = %s
            LIMIT 1
            """,
            (project_id,),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        resolved = str(row[0]).strip()
        return resolved or None

    def log_scan_event(
        self,
        findings: list[ScanFinding],
        surface: str,
        *,
        project_id: Any | None = None,
        job_id: Any | None = None,
        scan_report: ScanReport | None = None,
        actor_id: str | None = None,
        account_id: str | None = None,
    ) -> int | None:
        """Insert a security_event row + linked security_findings rows.

        Returns the event ID on success, None on failure.
        """
        if not findings:
            return None

        conn = self._conn()
        if conn is None:
            return None

        try:
            # Determine worst severity and action
            severity_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            action_rank = {"allow": 0, "warn": 1, "redact": 2, "block": 3}
            worst = max(
                findings,
                key=lambda f: (severity_rank.get(f.severity, 0), action_rank.get(f.action, 0)),
            )
            worst_severity = worst.severity
            worst_action = worst.action
            primary_scanner = worst.scanner
            primary_rule_id = worst.rule_id

            with conn.cursor() as cur:
                resolved_account_id = self._resolve_write_account_id(
                    cur,
                    account_id=account_id,
                    project_id=project_id,
                )
                if resolved_account_id is None:
                    return None
                cur.execute(
                    """
                    INSERT INTO security_events
                        (account_id, project_id, job_id, surface, scanner, rule_id, severity, action,
                         redacted_preview, trace_id, actor_id, scan_id, policy_level, duration_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        resolved_account_id,
                        project_id,
                        job_id,
                        surface,
                        primary_scanner,
                        primary_rule_id,
                        worst_severity,
                        worst_action,
                        findings[0].redacted_preview[:500] if findings else None,
                        findings[0].trace_id if findings else None,
                        actor_id,
                        scan_report.scan_id if scan_report else None,
                        scan_report.policy_level if scan_report else None,
                        scan_report.duration_ms if scan_report else None,
                    ),
                )
                event_id = cur.fetchone()[0]

                # Insert individual findings
                for f in findings:
                    cur.execute(
                        """
                        INSERT INTO security_findings
                            (event_id, scanner, rule_id, severity, confidence, context, redacted_snippet)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            event_id,
                            f.scanner,
                            f.rule_id,
                            f.severity,
                            f.confidence,
                            _safe_json(f.context),
                            f.redacted_preview[:500],
                        ),
                    )

            conn.commit()
            return event_id

        except Exception:
            logger.debug("log_scan_event failed", exc_info=True)
            with suppress(Exception):
                conn.rollback()
            return None
        finally:
            self._put_conn(conn)

    def log_policy_change(
        self,
        *,
        project_id: Any | None,
        actor_id: str,
        before: dict,
        after: dict,
        account_id: str | None = None,
    ) -> int | None:
        """Record a policy change as a security event."""
        conn = self._conn()
        if conn is None:
            return None

        try:
            with conn.cursor() as cur:
                resolved_account_id = self._resolve_write_account_id(
                    cur,
                    account_id=account_id,
                    project_id=project_id,
                )
                if resolved_account_id is None:
                    return None
                cur.execute(
                    """
                    INSERT INTO security_events
                        (account_id, project_id, surface, scanner, rule_id, severity, action,
                         redacted_preview, actor_id)
                    VALUES (%s, %s, 'policy', 'policy', 'policy:change', 'low', 'allow', %s, %s)
                    RETURNING id
                    """,
                    (
                        resolved_account_id,
                        project_id,
                        f"Policy changed from {before.get('level', '?')} to {after.get('level', '?')}",
                        actor_id,
                    ),
                )
                event_id = cur.fetchone()[0]
            conn.commit()
            return event_id
        except Exception:
            logger.debug("log_policy_change failed", exc_info=True)
            with suppress(Exception):
                conn.rollback()
            return None
        finally:
            self._put_conn(conn)

    def log_policy_resolution_error(
        self,
        *,
        project_id: Any | None,
        job_id: Any | None,
        surface: str,
        mode: str,
        message: str,
        account_id: str | None = None,
    ) -> int | None:
        """Record policy resolution failures as first-class security events."""
        conn = self._conn()
        if conn is None:
            return None

        normalized_mode = str(mode or "").strip().lower() or "monitor"
        if normalized_mode in {"enforce", "monitor_fail_closed"}:
            severity = "high"
            action = "block"
        else:
            severity = "medium"
            action = "warn"

        preview = str(message or "").strip() or "Security policy resolution failed"
        preview = preview[:500]
        rule_id = "policy:resolution_error"

        try:
            with conn.cursor() as cur:
                resolved_account_id = self._resolve_write_account_id(
                    cur,
                    account_id=account_id,
                    project_id=project_id,
                )
                if resolved_account_id is None:
                    return None
                cur.execute(
                    """
                    INSERT INTO security_events
                        (account_id, project_id, job_id, surface, scanner, rule_id, severity, action,
                         redacted_preview, actor_id)
                    VALUES (%s, %s, %s, %s, 'policy', %s, %s, %s, %s, 'system')
                    RETURNING id
                    """,
                    (
                        resolved_account_id,
                        project_id,
                        job_id,
                        str(surface or "policy"),
                        rule_id,
                        severity,
                        action,
                        preview,
                    ),
                )
                event_id = cur.fetchone()[0]
            conn.commit()
            return event_id
        except Exception:
            logger.debug("log_policy_resolution_error failed", exc_info=True)
            with suppress(Exception):
                conn.rollback()
            return None
        finally:
            self._put_conn(conn)

    def list_events(
        self,
        *,
        account_id: str | None = None,
        project_id: Any | None = None,
        job_id: Any | None = None,
        severity: str | None = None,
        surface: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Query security events with optional filters."""
        conn = self._conn()
        if conn is None:
            return []
        try:
            conditions: list[Any] = []
            params: list[Any] = []
            if account_id is not None:
                conditions.append(psycopg_sql.SQL("account_id = %s"))
                params.append(account_id)
            if project_id is not None:
                conditions.append(psycopg_sql.SQL("project_id = %s"))
                params.append(project_id)
            if job_id is not None:
                conditions.append(psycopg_sql.SQL("job_id = %s"))
                params.append(job_id)
            if severity:
                conditions.append(psycopg_sql.SQL("severity = %s"))
                params.append(severity)
            if surface:
                conditions.append(psycopg_sql.SQL("surface = %s"))
                params.append(surface)

            where_clause = psycopg_sql.SQL("")
            if conditions:
                where_clause = psycopg_sql.SQL("WHERE ") + psycopg_sql.SQL(" AND ").join(conditions)
            params.extend([limit, offset])

            with conn.cursor() as cur:
                cur.execute(
                    psycopg_sql.SQL(
                        """
                    SELECT id, account_id, project_id, job_id, surface, scanner, rule_id,
                           severity, action, redacted_preview, trace_id, scan_id, policy_level,
                           duration_ms, created_at
                      FROM security_events
                      {where_clause}
                     ORDER BY created_at DESC
                     LIMIT %s OFFSET %s
                    """
                    ).format(where_clause=where_clause),
                    params,
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
        except Exception:
            logger.debug("list_events failed", exc_info=True)
            return []
        finally:
            self._put_conn(conn)

    def list_findings(
        self,
        *,
        account_id: str | None = None,
        project_id: Any | None = None,
        event_id: str | None = None,
        scanner: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query security findings with optional filters (account-scoped)."""
        conn = self._conn()
        if conn is None:
            return []
        try:
            conditions: list[Any] = []
            params: list[Any] = []
            # Join through security_events for account/project scoping
            use_join = account_id is not None or project_id is not None
            if event_id is not None:
                conditions.append(psycopg_sql.SQL("sf.event_id = %s"))
                params.append(str(event_id))
            if scanner:
                conditions.append(psycopg_sql.SQL("sf.scanner = %s"))
                params.append(scanner)
            if account_id is not None:
                conditions.append(psycopg_sql.SQL("se.account_id = %s"))
                params.append(account_id)
                use_join = True
            if project_id is not None:
                conditions.append(psycopg_sql.SQL("se.project_id = %s"))
                params.append(project_id)
                use_join = True

            where_clause = psycopg_sql.SQL("")
            if conditions:
                where_clause = psycopg_sql.SQL("WHERE ") + psycopg_sql.SQL(" AND ").join(conditions)
            params.extend([limit, offset])

            if use_join:
                query = psycopg_sql.SQL(
                    """
                    SELECT sf.id, sf.event_id, sf.scanner, sf.rule_id, sf.severity,
                           sf.confidence, sf.context, sf.redacted_snippet, sf.created_at
                      FROM security_findings sf
                      JOIN security_events se ON se.id = sf.event_id
                      {where_clause}
                     ORDER BY sf.created_at DESC
                     LIMIT %s OFFSET %s
                    """
                ).format(where_clause=where_clause)
            else:
                query = psycopg_sql.SQL(
                    """
                    SELECT sf.id, sf.event_id, sf.scanner, sf.rule_id, sf.severity,
                           sf.confidence, sf.context, sf.redacted_snippet, sf.created_at
                      FROM security_findings sf
                      {where_clause}
                     ORDER BY sf.created_at DESC
                     LIMIT %s OFFSET %s
                    """
                ).format(where_clause=where_clause)

            with conn.cursor() as cur:
                cur.execute(query, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
        except Exception:
            logger.debug("list_findings failed", exc_info=True)
            return []
        finally:
            self._put_conn(conn)

    def count_findings_by_severity(
        self,
        project_id: Any,
        *,
        hours: int = 24,
    ) -> dict[str, int]:
        """Count findings by severity for a project in the last N hours."""
        conn = self._conn()
        if conn is None:
            return {}
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT se.severity, COUNT(sf.id)
                    FROM security_events se
                    JOIN security_findings sf ON sf.event_id = se.id
                    WHERE se.project_id = %s
                      AND se.created_at > NOW() - INTERVAL '%s hours'
                    GROUP BY se.severity
                    """,
                    (project_id, hours),
                )
                return dict(cur.fetchall())
        except Exception:
            return {}
        finally:
            self._put_conn(conn)

    def get_latest_posture_snapshot(
        self,
        *,
        project_id: Any | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any] | None:
        conn = self._conn()
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                normalized_account_id = str(account_id or "").strip()
                if project_id is not None:
                    if normalized_account_id:
                        cur.execute(
                            """
                            SELECT score, level, category_scores, finding_counts, created_at
                            FROM security_posture_snapshots
                            WHERE project_id = %s AND account_id = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (project_id, normalized_account_id),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT score, level, category_scores, finding_counts, created_at
                            FROM security_posture_snapshots
                            WHERE project_id = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (project_id,),
                        )
                elif normalized_account_id:
                    cur.execute(
                        """
                        SELECT score, level, category_scores, finding_counts, created_at
                        FROM security_posture_snapshots
                        WHERE account_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (normalized_account_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT score, level, category_scores, finding_counts, created_at
                        FROM security_posture_snapshots
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "score": row[0],
                    "level": row[1],
                    "category_scores": row[2],
                    "finding_counts": row[3],
                    "created_at": row[4],
                }
        except Exception:
            logger.debug("get_latest_posture_snapshot failed", exc_info=True)
            return None
        finally:
            self._put_conn(conn)

    def save_posture_snapshot(
        self,
        *,
        project_id: Any,
        score: int,
        level: str,
        category_scores: dict[str, int],
        finding_counts: dict[str, int],
        account_id: str | None = None,
    ) -> bool:
        conn = self._conn()
        if conn is None:
            return False
        try:
            import json

            with conn.cursor() as cur:
                resolved_account_id = self._resolve_write_account_id(
                    cur,
                    account_id=account_id,
                    project_id=project_id,
                )
                if resolved_account_id is None:
                    return False
                cur.execute(
                    """
                    INSERT INTO security_posture_snapshots
                        (account_id, project_id, score, level, category_scores, finding_counts)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resolved_account_id,
                        project_id,
                        score,
                        level,
                        json.dumps(category_scores),
                        json.dumps(finding_counts),
                    ),
                )
            conn.commit()
            return True
        except Exception:
            logger.debug("save_posture_snapshot failed", exc_info=True)
            with suppress(Exception):
                conn.rollback()
            return False
        finally:
            self._put_conn(conn)


def _safe_json(obj: Any) -> str | None:
    """Convert to JSON string safely."""
    import json

    try:
        return json.dumps(obj)
    except Exception:
        return None
