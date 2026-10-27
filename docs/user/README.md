# User Documentation

This section contains user-facing release documentation for Flow Steward. It is
separate from architecture notes, standards, and operations runbooks.

Start here:

1. [Feature Catalog](feature-catalog.md) - product capability inventory and documentation scope.
2. [Workflows](workflows.md) - workflow builder, steps, groups, the execution graph, Approval and Wait steps, change sets, contracts, test runs, the run inspector, webhooks, artifacts, and notifications.
3. [Configuration Reference](configuration-reference.md) - user/operator settings by feature area.

Supporting references:

- [Main User Guide](../GUIDE.md)
- [Generated Environment Catalog](../configuration/flow-steward-env-catalog.md)
- [Security Runbook](../security-runbook.md)

Audience map:

- People should start with `README.md`, `docs/GUIDE.md`, and `docs/user/*`.
- Operators should use the generated environment catalog for env-variable
  ownership, defaults, examples, and runtime read locations.
- API clients should use `/openapi-public.json` or `/openapi-internal.json`
  when the matching docs flags are enabled.
- Agents and IDEs should treat MCP `tools/list` as the exact tool schema source
  and use this guide only as a navigation layer. For authenticated HTTP clients,
  discovery is request-local and returns only tools allowed by the current
  token scopes; direct calls are checked again at execution time.

For current user/operator behavior, prefer `docs/user/*`, `README.md`, and `docs/GUIDE.md` over older architecture or testing notes.
