# pip-audit Risk Acceptance

Last reviewed: 2026-10-23

## Accepted Vulnerabilities

### mcp / GHSA-84m7-p3x7-pcfv, CVE-2026-59951

- Audit label: semgrep
- Accepted until: 2026-10-30
- Scope: isolated Semgrep audit-tool environment only
- Owner: Flow Steward maintainers
- Status: temporary acceptance pending a compatible Semgrep release

Stateful Streamable HTTP sessions are never reclaimed and are unlimited, so an
MCP server's memory can grow until restart (published 2026-10-15; fixed in
1.30.0 and 2.2.0). The product images install mcp 2.2.0 from the lock files.
Semgrep 1.179.0, and every release through 1.181.0 (2026-10-14), pins
`mcp==1.29.0` exactly, so its isolated tool environment cannot take the fix.
The flaw is in the MCP server transport; Flow Steward runs Semgrep only as an
offline static-analysis CLI against the checked-out source tree and never
starts `semgrep mcp`. Remove this acceptance as soon as a Semgrep release
permits mcp 1.30.0 or later.

### opentelemetry-api / GHSA-xcq7-p3pf-p88h

- Audit label: semgrep
- Accepted until: 2026-11-06
- Scope: isolated Semgrep audit-tool environment only
- Owner: Flow Steward maintainers
- Status: temporary acceptance pending a compatible Semgrep release

The W3C `tracestate` header is parsed without a bound, so a crafted header can
cost CPU and memory (published 2026-10-22; fixed in 1.45.1). Semgrep 1.179.0
requires `opentelemetry-api~=1.37.0`, so its isolated tool environment cannot
take the fix. The product images install 1.45.1 from the lock files. Semgrep runs only as an offline CLI against the checked-out
source tree and never parses an incoming trace header. Remove this acceptance
as soon as a Semgrep release permits opentelemetry-api 1.45.1 or later.

## Rationale

- Semgrep 1.179.0 requires `pyjwt[crypto]>=2.15.0`, so its isolated tool
  environment installs the patched PyJWT and the temporary Semgrep PyJWT
  acceptances (including GHSA-x33g-cr3x-6449, published 2026-10-05) were
  removed rather than extended.
- Fixed upstream releases are preferred over carrying risk acceptance.

## Temporary Compensating Controls

- Re-run `python -m pip_audit` in CI/nightly and add a time-bound acceptance only
  when no fixed upstream release is available or a runtime upgrade is explicitly
  blocked by compatibility evidence.
- Keep every acceptance time-bound and remove it once the upstream dependency
  set resolves to a fixed release.
- Keep Semgrep offline and do not expose any MCP transport from its isolated
  environment while its exact transitive pin remains vulnerable.
