# Qualify an adapter through the real executor

Use for an HTTP/MCP integration and when the user requests offline execution.
Refresh installed native, extension and MCP operations first, as described in
[integration selection](integration-selection.md). These search examples do not
establish that HTTP is preferable to a connector available on another installation.

## Establish the contract before the fixture

Record dated official documentation or the exact installed operation schema,
authentication ownership, request arguments, response fields, empty-result
semantics, error statuses and pagination. Inspect runtime projection too: graph
HTTP `response_target: state.search` stores the raw response body there. Do not
invent a `response_body` wrapper. A shape check for `mapping` alone does not
qualify a provider response. Check the envelope and the fields actually consumed.
Where documentation conflicts, name the validated subset rather than claiming
complete provider-schema validation. Fixtures are synthetic examples derived from
contracts, never evidence that a provider accepted the request.

For OAuth integrations, a fixture access token cannot establish that consent,
redirects, token exchange and renewal exist. Those must be implemented by a
qualified connection provider; leave a missing adapter as a blocker.

## Execute with replacement at external boundaries

Use the version-matched production executor and its input admission, state
projection, routing, retry policy, terminal handling and output validation.
Replace provider dispatch with fixtures; do not replace business nodes, output
selectors or the result under test. Disable networking at the container level
and forbid socket connections. Use only synthetic secrets and verify their
absence in final state, checkpoints, output and diagnostics. Inspect serialized
requests, including authentication placement, exact JSON types, query encoding
and redirect behavior. A request-contract assertion must fail the test even if
the workflow catches the transport exception and routes to an expected failure.

Exercise successful and empty results; missing/wrong response fields; missing
inputs; authentication/quota/server failures; redirects; invalid JSON; timeouts;
response size limits, including compressed bodies; and secret reflection.
Exercise child mappings through actual Invoke execution when qualifying a parent:
a search child alone does not validate its callers. Record test failures before
fixing the corresponding YAML, and rerun the affected cases after edits.

Report exact YAML hashes, target source/version, executor module hashes, fixture
sources, counts and branch traces. Disclose replacement persistence, events,
secret codec and retry timing. A preselected graph with in-memory checkpoints
qualifies that execution scope, not UI routing, broker admission or database
persistence. Keep structural checks, engine-fixture checks and live-provider
checks as separate fields. Do not set an unqualified `runtime_verified=true`
from offline fixtures. No save, API call or publication follows implicitly.

## Reusable search regression harness

The [search examples](../assets/offline_examples/search/README.md) and
[`verify_search_workflows.py`](../scripts/verify_search_workflows.py) exercise
SerpAPI, Tavily and Exa through production LangGraph nodes. This is a maintainer
harness requiring a compatible Flow Steward checkout and its audit runtime;
installing the Operator skill alone does not supply that runtime. It does not
change core. The ordinary converter still uses its small pinned requirements.

Inside an isolated, version-matched audit container with `--network none`, set
`PYTHONPATH` to the mounted checkout and development environment variables, then:

```bash
python <skill-dir>/scripts/verify_search_workflows.py \
  --bundle-dir <skill-dir>/assets/offline_examples/search \
  --contracts <skill-dir>/assets/offline_examples/search/provider-contracts.json \
  --fixture-public-ip <public-fixture-ip>
```

The DNS fixture is a public IPv4 test value supplied by the maintainer so the
real SSRF guard runs unchanged; the transport is still replaced and sockets
remain forbidden. It is never contacted.

Exit 0 means the selected fixtures passed; it does not certify live access.
Use `--case provider:case` for a focused regression. Keep any real credentials
out of the container. If compatible execution is unavailable, deliver the YAML
with that verification explicitly pending rather than simulating a passing run.
