# Security Scanning Runbook

## Feature Flags

| Flag | Values | Default |
|------|--------|---------|
| `SECURITY_SCAN_MODE` | `monitor`, `enforce_standard`, `enforce_strict`, `disabled` | `monitor` |
| `SECURITY_FAIL_CLOSED_ON_POLICY_ERROR` | `0`, `1` | `0` |
| `SECURITY_PII_SALT` | Hex string (32+ bytes) | `default-pii-salt-change-me` |

### Modes

- **monitor** — All scanners run, findings are logged to `security_events`, and scan-path redaction still applies when detectors trigger. Policy-resolution errors pass through unless `SECURITY_FAIL_CLOSED_ON_POLICY_ERROR=1`.
- **enforce_standard** — Standard policy applied. Secrets redacted, injection blocked (high+), PII pseudonymized.
- **enforce_strict** — Strict policy. Lower thresholds, hard-redact PII, all contexts scanned.
- **disabled** — Scanning completely off. No overhead.

## Rollout Procedure

1. Deploy with `SECURITY_SCAN_MODE=monitor`
2. Monitor `security_events` table and `/api/security/events` endpoint for false positives
3. Tune allowlists and custom rules via `/api/security/projects/{id}/rules`
4. When confident, switch to `SECURITY_SCAN_MODE=enforce_standard`
5. For high-security projects, use `enforce_strict`
6. Optional hardening: set `SECURITY_FAIL_CLOSED_ON_POLICY_ERROR=1` to block payloads when policy resolution fails

## Rollback

Rollback does NOT require migration rollback:

```bash
# Immediate rollback to monitor-only
SECURITY_SCAN_MODE=monitor docker compose restart fs-app

# Or disable completely
SECURITY_SCAN_MODE=disabled docker compose restart fs-app
```

## Incident Response: Secret Leak Detected

1. **Detect** — Alert from `security_events` with `scanner=secrets`, `severity=critical`
2. **Identify** — Check `redacted_preview` and `rule_id` to determine secret type
3. **Revoke** — Immediately rotate the compromised credential at the provider
4. **Verify** — Confirm the old credential is invalid
5. **Audit** — Review `security_events` for scope of exposure (which jobs, surfaces)
6. **Remediate** — Update the secret in the secure store, redeploy

## Incident Response: PII Exposure

1. **Detect** — Alert from `security_events` with `scanner=pii`
2. **Scope** — Check if PII was stored in `security_events.redacted_preview` (should be pseudonymized, never raw)
3. **Verify** — Confirm no raw PII in `security_findings.redacted_snippet`
4. **Escalate** — If raw PII found, escalate to data protection officer

## Monitoring Queries

```sql
-- Recent critical findings
SELECT * FROM security_events
WHERE severity = 'critical'
ORDER BY created_at DESC LIMIT 20;

-- Finding counts by scanner (24h)
SELECT scanner, severity, COUNT(*)
FROM security_events
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY scanner, severity
ORDER BY scanner, severity;

-- Projects with most findings
SELECT project_id, COUNT(*) as finding_count
FROM security_events
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY project_id
ORDER BY finding_count DESC;
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/security/policy` | any | Global policy |
| PUT | `/api/security/policy` | admin | Update global policy |
| GET | `/api/security/projects/{id}/policy` | any | Project policy (merged) |
| PUT | `/api/security/projects/{id}/policy` | operator+ | Update project override |
| POST | `/api/security/projects/{id}/rules` | operator+ | Add custom rule |
| GET | `/api/security/posture` | any | Posture score |
| GET | `/api/security/events` | any | Event list (paginated) |
| GET | `/api/security/findings` | any | Findings list (paginated) |
| GET | `/security` | any | Security dashboard panel |

## Database Tables

- `security_events` — One row per scan that produced findings
- `security_findings` — Detailed per-rule findings (FK to events)
- `security_posture_snapshots` — Periodic health score snapshots
