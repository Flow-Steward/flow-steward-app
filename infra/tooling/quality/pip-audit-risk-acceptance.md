# pip-audit Risk Acceptance

## Accepted Vulnerabilities

No vulnerability acceptances are currently configured.

The isolated audit scanner uses `semgrep==1.183.0+fs.1`, built from verified
upstream code with corrected dependency requirements (ADR 0049). MCP and
OpenTelemetry are audited at their actual installed, patched versions. The
unchanged Semgrep code is audited as upstream 1.183.0 after provenance checks.
The previous MCP and OpenTelemetry tool-environment exceptions are removed;
no expiry was extended and no advisory is suppressed.
