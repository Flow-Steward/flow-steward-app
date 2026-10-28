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
The flaw is in the MCP server transport; Flow Steward runs Semgrep only as a
static-analysis CLI against the checked-out source tree, with configured rule
registry downloads permitted and no MCP SDK client or server transport startup.
Remove this acceptance as soon as a Semgrep release permits mcp 1.30.0 or later.

### mcp / GHSA-5h93-6whr-6q8j

- Audit label: semgrep
- Accepted until: 2026-10-30
- Scope: isolated Semgrep audit-tool environment only
- Owner: Flow Steward maintainers
- Status: temporary acceptance pending a compatible Semgrep release

The SDK's HTTP client transports can forward custom headers and request bodies
across origins on redirects. The fix is mcp 1.30.0 or 2.2.0. Checked 2026-10-28:
Semgrep 1.179.0 and the latest release 1.182.0 pin `mcp==1.29.0` exactly.
The quality gate invokes the default scan command, which does not open MCP
client sessions or transports. Registry rule downloads use Semgrep's HTTP
session, not the MCP SDK transport. Product images already install mcp 2.2.0.
This acceptance shares the existing MCP acceptance's expiry without extending it;
remove it when Semgrep permits the fixed dependency or before enabling MCP use.

Sources: [maintainer advisory](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-5h93-6whr-6q8j),
[Semgrep dependency pin](https://github.com/semgrep/semgrep/blob/v1.182.0/cli/pyproject.toml),
[scan dispatch](https://github.com/semgrep/semgrep/blob/v1.179.0/cli/src/semgrep/cli.py),
[rule downloads](https://github.com/semgrep/semgrep/blob/v1.179.0/cli/src/semgrep/config_resolver.py).

### mcp / GHSA-rwrf-2pqf-9j8j

- Audit label: semgrep
- Accepted until: 2026-10-30
- Scope: isolated Semgrep audit-tool environment only
- Owner: Flow Steward maintainers
- Status: temporary acceptance pending a compatible Semgrep release

An MCP client validating server-provided tool output schemas can fetch external
`$ref` URLs, causing unintended network or local-file access and event-loop stalls.
The fix is mcp 1.30.0 or 2.2.0. Checked 2026-10-28: Semgrep 1.179.0 and the
latest release 1.182.0 pin `mcp==1.29.0` exactly. The quality scan does not
instantiate an MCP client or call remote MCP tools; its scan results are not
MCP tool results. Product images already install mcp 2.2.0. This acceptance
shares the existing MCP acceptance's expiry without extending it; remove it
when Semgrep permits the fixed dependency or before enabling MCP use.

Sources: [maintainer advisory](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-rwrf-2pqf-9j8j),
[Semgrep dependency pin](https://github.com/semgrep/semgrep/blob/v1.182.0/cli/pyproject.toml),
[scan command](https://github.com/semgrep/semgrep/blob/v1.179.0/cli/src/semgrep/commands/scan.py),
[scan implementation](https://github.com/semgrep/semgrep/blob/v1.179.0/cli/src/semgrep/run_scan.py).

### mcp / GHSA-w4fh-qvv9-3v23

- Audit label: semgrep
- Accepted until: 2026-10-30
- Scope: isolated Semgrep audit-tool environment only
- Owner: Flow Steward maintainers
- Status: temporary acceptance pending a compatible Semgrep release

The SDK's server bearer authentication can accept a token for another resource
without audience validation. The fixed releases are mcp 1.30.0 and 2.2.0;
servers using SDK authentication also need the advisory's audience-validation
configuration. Checked 2026-10-28: Semgrep 1.179.0 and the latest release 1.182.0
pin `mcp==1.29.0` exactly. The quality scan does not invoke `semgrep mcp`,
construct its FastMCP server, expose MCP transports, or accept bearer tokens.
Product images already install mcp 2.2.0 and are outside this acceptance.
This acceptance shares the existing MCP acceptance's expiry without extending it;
remove it when Semgrep permits the fixed dependency or before enabling MCP use.

Sources: [maintainer advisory](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-w4fh-qvv9-3v23),
[Semgrep dependency pin](https://github.com/semgrep/semgrep/blob/v1.182.0/cli/pyproject.toml),
[MCP command dispatch and server initialization](https://github.com/semgrep/semgrep/blob/v1.179.0/cli/src/semgrep/commands/mcp.py).

### opentelemetry-api / GHSA-xcq7-p3pf-p88h

- Audit label: semgrep
- Accepted until: 2026-11-06
- Scope: isolated Semgrep audit-tool environment only
- Owner: Flow Steward maintainers
- Status: temporary acceptance pending a compatible Semgrep release

The W3C `tracestate` header is parsed without a bound, so a crafted header can
cost CPU and memory (published 2026-10-22; fixed in 1.45.1). Semgrep 1.179.0
requires `opentelemetry-api~=1.37.0`, so its isolated tool environment cannot
take the fix. The product images install 1.45.1 from the lock files. Semgrep runs
only as a static-analysis CLI against the checked-out source tree, with configured
rule registry downloads permitted and no MCP SDK client or server transport
startup; it never parses an incoming trace header. Remove this acceptance as
soon as a Semgrep release permits opentelemetry-api 1.45.1 or later.

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
- Use Semgrep only as a static-analysis CLI, permitting configured rule registry
  downloads, with no MCP SDK client or server transport startup from its isolated
  environment while its exact transitive pin remains vulnerable.
