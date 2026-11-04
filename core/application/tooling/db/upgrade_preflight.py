"""Read-only preflight of `database upgrade` (ADR 0056).

Everything here only reads. `blocking` entries stop the upgrade before any change;
`warnings` are reported and do not. No entry carries a secret value, a ciphertext or a
query text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.application.tooling.db import migration_runner, upgrade_phases
from core.infrastructure.config.session_identity import NON_BLOCKING_ROLES, SESSION_PREFIX

_ROOT = Path(__file__).resolve().parents[4]
_LISTED = 20


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **({"details": details} if details else {})}


def _released_baselines(root: Path) -> set[str]:
    payload = json.loads((root / "db" / "released_migrations.json").read_text(encoding="utf-8"))
    released = {str(item) for item in payload.get("released_baseline_fingerprints") or []}
    released.add(str(payload.get("baseline_schema_fingerprint") or ""))
    return {item for item in released if item}


def _artifact_issues(root: Path) -> list[dict[str, Any]]:
    manifest = root / "db" / "released_migrations.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [
            _issue(
                "migration_artifact_invalid",
                "Release database artifact db/released_migrations.json is missing, unreadable, "
                "or invalid; restore the complete matching release before upgrading.",
            )
        ]
    released = payload.get("migrations") if isinstance(payload, dict) else None
    if not isinstance(released, dict) or any(
        not migration_runner.is_valid_migration_filename(name)
        or not isinstance(checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        for name, checksum in released.items()
    ):
        return [
            _issue(
                "migration_artifact_invalid",
                "Release database artifact db/released_migrations.json has an invalid migration "
                "inventory; restore the complete matching release before upgrading.",
            )
        ]
    try:
        files = {path.name: path for path in migration_runner.discover_migration_files(root)}
    except (OSError, migration_runner.MigrationError) as error:
        return [
            _issue("migration_artifact_invalid", f"Release migration inventory is invalid: {error}")
        ]
    issues = []
    for name in sorted(set(released) - set(files)):
        issues.append(
            _issue(
                "migration_artifact_invalid",
                f"Release database artifact db/migrations/{name} is missing.",
            )
        )
    for name in sorted(set(files) - set(released)):
        issues.append(
            _issue(
                "migration_artifact_invalid",
                f"Release database artifact db/migrations/{name} is not recorded in "
                "db/released_migrations.json.",
            )
        )
    for name in sorted(set(released) & set(files)):
        try:
            checksum = migration_runner.migration_checksum(files[name].read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            issues.append(
                _issue(
                    "migration_artifact_invalid",
                    f"Release database artifact db/migrations/{name} is unreadable.",
                )
            )
            continue
        if checksum != released[name]:
            issues.append(
                _issue(
                    "migration_artifact_invalid",
                    f"Release database artifact db/migrations/{name} does not match its checksum "
                    "in db/released_migrations.json.",
                )
            )
    return issues


def validate_release_artifacts(root: Path | None = None) -> None:
    """Refuse an incomplete release before the installer can apply baseline migrations."""
    issues = _artifact_issues(Path(root) if root is not None else _ROOT)
    if issues:
        raise RuntimeError(issues[0]["message"])


def ledger_gaps(ordered: list[str], applied: set[str]) -> list[str]:
    """This release's files missing before the last one the ledger records (ADR 0056).

    A supported source has applied a prefix of the ordered files; running an earlier
    file after a later one would apply it out of order. Names this release does not
    know are reported as newer or pre-release instead, so they are left out here.
    """
    last = max((index for index, name in enumerate(ordered) if name in applied), default=-1)
    return [name for name in ordered[: last + 1] if name not in applied]


def _table_exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    row = cur.fetchone()
    return bool(row and row[0])


def _source_issues(conn: Any, root: Path) -> list[dict[str, Any]]:
    from core.application.tooling.db.upgrade_db import pre_release_migrations

    issues: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        if not _table_exists(cur, "platform_schema_migrations"):
            return issues
        if _table_exists(cur, "platform_schema_state"):
            cur.execute(
                "SELECT schema_fingerprint FROM platform_schema_state ORDER BY updated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            recorded = str(row[0] if row else "").strip()
            if recorded and recorded not in _released_baselines(root):
                issues.append(
                    _issue(
                        "unknown_schema",
                        "The database records a schema baseline no published release shipped.",
                        recorded_fingerprint=recorded,
                    )
                )
    unknown = migration_runner.applied_unknown_migrations(conn, root=root)
    pre_release = pre_release_migrations(unknown)
    if pre_release:
        issues.append(
            _issue(
                "pre_release_database",
                "The migration ledger comes from a pre-release build; recreate the database.",
                migrations=pre_release[:_LISTED],
            )
        )
    newer = sorted(set(unknown) - set(pre_release))
    if newer:
        issues.append(
            _issue(
                "newer_database",
                "A newer release has upgraded this database; restore the backup taken before it.",
                migrations=newer[:_LISTED],
            )
        )
    drifted = migration_runner.inspect_migrations(conn, root=root)["drifted"]
    if drifted:
        issues.append(
            _issue(
                "migration_drift",
                "Applied migrations differ from this release's files.",
                migrations=drifted[:_LISTED],
            )
        )
    skipped = ledger_gaps(
        [path.name for path in migration_runner.discover_migration_files(root)],
        set(migration_runner.fetch_applied_migrations(conn)),
    )
    if skipped:
        issues.append(
            _issue(
                "unknown_schema",
                "The applied migrations are not a prefix of this release's ordered files: "
                "a later one is recorded while an earlier one is not.",
                missing_migrations=skipped[:_LISTED],
            )
        )
    unknown_phases = upgrade_phases.unknown_recorded_phases(conn)
    if unknown_phases:
        issues.append(
            _issue(
                "newer_database",
                "A newer release ran upgrade phases this release does not know.",
                phases=unknown_phases[:_LISTED],
            )
        )
    return issues


def _session_issues(conn: Any, *, allow_unlabeled_sessions: bool) -> tuple[list, list]:
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pid, COALESCE(application_name, ''), usename = current_user
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND pid <> pg_backend_pid()
               AND backend_type = 'client backend'
            """
        )
        sessions = cur.fetchall() or []
    labelled = []
    unlabelled = []
    for pid, name, same_user in sessions:
        parts = str(name).split(":")
        if parts[0] == SESSION_PREFIX and len(parts) >= 2:
            if parts[1] not in NON_BLOCKING_ROLES:
                labelled.append({"pid": pid, "application_name": name})
        elif same_user:
            unlabelled.append({"pid": pid, "application_name": name})
    if labelled:
        blocking.append(
            _issue(
                "runtime_sessions",
                "Flow Steward processes are still connected; stop every app, worker and "
                "scheduler process first.",
                sessions=labelled[:_LISTED],
            )
        )
    if unlabelled:
        # Releases before this one do not name their sessions, so an unnamed session
        # of the application's own database user may be one of them.
        entry = _issue(
            "unlabeled_sessions",
            "Sessions of the application's database user carry no Flow Steward name; "
            "they may be processes of an earlier release.",
            sessions=unlabelled[:_LISTED],
        )
        (warnings if allow_unlabeled_sessions else blocking).append(entry)
    return blocking, warnings


def _catalog_move_issues(conn: Any) -> tuple[list, list]:
    """What the secret catalog move (ADR 0057) cannot do on its own."""
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    applied = set(migration_runner.fetch_applied_migrations(conn)) if _ledger(conn) else set()
    if "20261103090000_secret_catalog.sql" in applied:
        return blocking, warnings
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH refs AS (
                SELECT pc.connection_id, pc.account_id, pc.project_id,
                       r.key AS field_name, btrim(r.value) AS ref_key,
                       EXISTS (
                           SELECT 1 FROM secret_refs AS s
                            WHERE s.account_id = pc.account_id AND s.ref_key = btrim(r.value)
                       ) AS has_value
                  FROM project_connections AS pc
                 CROSS JOIN LATERAL jsonb_each_text(
                       CASE WHEN jsonb_typeof(pc.secret_refs -> 'refs') = 'object'
                            THEN pc.secret_refs -> 'refs' ELSE '{}'::jsonb END) AS r
                 WHERE r.key NOT LIKE '\\_oauth\\_%%' AND btrim(r.value) <> ''
            )
            , sharing AS (
                SELECT account_id, ref_key, count(DISTINCT project_id) AS projects
                  FROM refs
                 GROUP BY account_id, ref_key
            )
            SELECT refs.connection_id, refs.field_name, refs.has_value, sharing.projects
              FROM refs
              JOIN sharing
                ON sharing.account_id = refs.account_id AND sharing.ref_key = refs.ref_key
             ORDER BY refs.connection_id, refs.field_name
            """
        )
        rows = cur.fetchall() or []
    shared = [
        {"connection_id": cid, "field": field}
        for cid, field, has_value, projects in rows
        if has_value and int(projects) > 1
    ]
    missing = [
        {"connection_id": cid, "field": field}
        for cid, field, has_value, _p in rows
        if not has_value
    ]
    if shared:
        blocking.append(
            _issue(
                "secret_catalog_shared_value",
                "A stored connection credential is shared by connections of different "
                "projects; enter it separately per connection before upgrading.",
                references=shared[:_LISTED],
                total=len(shared),
            )
        )
    if missing:
        warnings.append(
            _issue(
                "secret_catalog_missing_value",
                "Connection credentials reference a value that does not exist; they stay "
                "unbound and must be entered again.",
                references=missing[:_LISTED],
                total=len(missing),
            )
        )
    return blocking, warnings


def _ledger(conn: Any) -> bool:
    with conn.cursor() as cur:
        return _table_exists(cur, "platform_schema_migrations")


def preflight(
    conn: Any, *, allow_unlabeled_sessions: bool = False, root: Path | None = None
) -> dict[str, Any]:
    repo_root = Path(root) if root is not None else _ROOT
    artifact_issues = _artifact_issues(repo_root)
    if artifact_issues:
        return {
            "blocking": artifact_issues,
            "warnings": [],
            "pending_migrations": [],
            "pending_phases": [],
        }
    blocking = _source_issues(conn, repo_root)
    session_blocking, warnings = _session_issues(
        conn, allow_unlabeled_sessions=allow_unlabeled_sessions
    )
    blocking.extend(session_blocking)
    move_blocking, move_warnings = _catalog_move_issues(conn)
    blocking.extend(move_blocking)
    warnings.extend(move_warnings)
    return {
        "blocking": blocking,
        "warnings": warnings,
        "pending_migrations": migration_runner.pending_migration_ids(conn, root=repo_root)
        if _ledger(conn)
        else [p.name for p in migration_runner.discover_migration_files(repo_root)],
        "pending_phases": [
            phase.phase_id
            for phase in upgrade_phases.UPGRADE_PHASES
            if phase.phase_id not in upgrade_phases.recorded_phases(conn)
        ],
    }


__all__ = ["ledger_gaps", "preflight", "validate_release_artifacts"]
