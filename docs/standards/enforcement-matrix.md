# Enforcement Matrix

This document maps mandatory rules to the mechanism that enforces them.

## Purpose

The goal of this matrix is to ensure that important rules are not only written down,
but also enforced through automation, review, or architecture governance.

## Enforcement Types

- `lint` — static checks such as Ruff, Bandit, Semgrep, mypy, or custom scripts
- `test` — unit, integration, UI, security, DB, or e2e verification
- `arch-test` — structural or dependency checks
- `review` — required reviewer attention
- `adr` — architecture decision record required before merge
- `workflow` — enforced through agent workflow or release gate

## Initial Baseline Matrix

| Rule | Source | Status | Enforcement Today | Owner | Gate |
|---|---|---|---|---|---|
| No new identifiers containing retired project codenames | `engineering-standard.md` | `Current` | `flow-steward quality check-naming` in Docker + unit tests + review | engineering | PR + local |
| Import ordering aligned with Ruff | `engineering-standard.md`, `backend-python-standard.md` | `Current` | `ruff check` (`I` rules) through `./core/entrypoints/cli/scripts/lint.sh` | engineering | local |
| Agents format only their changed Python/TypeScript files before Tier 1 tests and inspect the diff | `engineering-standard.md`, `agent-workflow-standard.md`, `AGENTS.md` | `Current` | agent workflow + review; Ruff and ESLint check mode in fast/full gates detects missed formatting | engineering | local |
| Public APIs typed | `engineering-standard.md`, `backend-python-standard.md` | `Planned` | mypy runs today, but there is no public-API annotation gate yet | backend | planned |
| Keep handlers thin and transport-focused | `architecture-standard.md`, `backend-python-standard.md` | `Current` | review on touched handlers; no repo-wide automated structure gate yet | backend | PR |
| Business logic lives outside transport handlers | `architecture-standard.md`, `backend-python-standard.md` | `Current` | review + targeted service/route tests in affected areas | backend | PR + local |
| New first-party code follows the canonical repository layout | `architecture-standard.md`, `backend-python-standard.md`, `agent-workflow-standard.md` | `Planned` | review today; future architecture tests should validate placement | architecture/backend | planned |
| Folders outside the canonical layout are transition-only | `architecture-standard.md`, `agent-workflow-standard.md` | `Planned` | review only today; no automated detector yet | architecture | planned |
| File-size / module-size review trigger | `engineering-standard.md` | `Planned` | documented review trigger only; no automated detector today | engineering | planned |
| Bug fixes require regression tests | `testing-standard.md` | `Current` | review + test suite expectations in changed areas | engineering | PR + local |
| Canonical job lifecycle and diagnostics contract stays aligned across UI, API, CLI, and MCP | `architecture-standard.md`, `testing-standard.md` | `Current` | `tests/unit/test_job_detail_contract.py`, `tests/unit/test_api_contracts_v2.py`, `tests/unit/test_cli_jobs.py`, `tests/unit/test_mcp_server_workflow_ops.py`, and `assets/frontend/src/pages/jobs/detail/index.test.tsx` | backend/frontend | PR + local |
| Observability vocabulary stays consistent across jobs, events, and dashboard surfaces | `architecture-standard.md`, `testing-standard.md` | `Current` | `tests/unit/test_query_service_runtime.py`, `tests/unit/test_query_services.py`, `assets/frontend/src/pages/events/api.test.ts`, and targeted dashboard/event UI checks in touched flows | backend/frontend | PR + local |
| Tier 1 uses regression, impact-based selection, and sequential fail-fast execution | `testing-standard.md`, `agent-workflow-standard.md` | `Current` | regression-test requirement + agent workflow + review | engineering | local + PR |
| Every tier, Tier 1 included, executes inside the disposable `fs-audit` container via `lint.sh --checks …`, never on a host interpreter | `testing-standard.md`, `agent-workflow-standard.md`, `AGENTS.md` | `Current` | `tests/unit/tooling/quality/test_check_registry.py` fails when a registry command escapes the gate | engineering | local |
| Tier 2 fast gate runs before intermediate commits | `testing-standard.md`, `agent-workflow-standard.md`, `AGENTS.md` | `Current` | pre-commit hook runs `lint.sh --fast`; agent workflow requires explicit execution | engineering | local |
| Tier 3 runs one clean full gate from the final committed state before handoff/PR/merge | `testing-standard.md`, `agent-workflow-standard.md`, `AGENTS.md` | `Current` | local `lint.sh --full` refreshes/reuses exact-SHA Tier 2, runs the Tier 3 delta, then validates cumulative evidence | engineering | local + PR + local |
| Candidate publication requires complete Tier 2 and Tier 3 evidence for the exact final SHA | `testing-standard.md`, `agent-workflow-standard.md`, `AGENTS.md` | `Current` | local release gate validates current ownership, fingerprints, commands, status, and commit identity for commit-scoped checks; input-scoped checks match the fingerprint of the final tree | engineering/release | release |
| Tier 3 backend units run without `-x` or `--maxfail=1`, retain the complete failure list, and report failures grouped by root cause | `testing-standard.md`, `agent-workflow-standard.md`, `AGENTS.md` | `Current` | `lint.sh --full` command contract + `tests/unit/test_lint_script.py` | engineering | local + PR + local |
| The Tier 3 backend unit suite runs on a fixed number of xdist workers, each against its own copy of the audit database; a test that cannot be isolated is pinned with `xdist_group("serial")` | `testing-standard.md`, ADR 0037 | `Current` | `lint.sh` command contract in `tests/unit/test_lint_script.py` + `tests/unit/test_xdist_isolation.py` | engineering | local |
| Quality check ownership and evidence reuse are single-owner, fingerprinted, and bound to the final SHA at release | `testing-standard.md`, `agent-workflow-standard.md`, ADR 0021, ADR 0036 | `Current` | typed check registry with per-check reuse scope, checksummed evidence shared through the quality cache volume, and final-SHA release validation | engineering/release | local + release |
| Coverage is collected once per full gate rather than during iteration or every shard | `testing-standard.md` | `Current` | local Tier 3 owner runs the backend suite once | engineering | local |
| Completion evidence identifies tier, exact commands, commit, candidate, and unrun checks | `testing-standard.md`, `agent-workflow-standard.md` | `Current` | agent workflow + reviewer sign-off | engineering/release | local + PR + release |
| Evidence from different commits or candidate environments is never combined as one green run | `testing-standard.md`, `agent-workflow-standard.md` | `Current` | completion-report review + release provenance checks | engineering/release | PR + release |
| Auth required by default | `security-standard.md` | `Current` | review + security/unit/e2e tests | backend/security | PR + local |
| Validate external input at boundaries | `security-standard.md`, `backend-python-standard.md` | `Current` | review + transport/security tests | backend/security | PR + local |
| Login and password-reset rate limiting | `security-standard.md` | `Current` | auth config + unit/e2e/security tests | backend/security | PR + local |
| Abuse-exposure review for bootstrap, token, webhook, and MCP ingress | `security-standard.md` | `Current` | review on touched ingress surfaces | backend/security | PR |
| Explicit broader public-ingress abuse controls | `security-standard.md` | `Planned` | partially covered today; no uniform ingress-wide enforcement | backend/security | planned |
| Extension manifests follow the canonical YAML-only manifest standard, including callback declarations | `extension-manifest-standard.yaml` | `Current` | `flow-steward extensions validate <extension-id> --json` + validator unit tests | backend/extensions | local |
| Extension scaffold generates YAML-only authoring bundles | `extension-manifest-standard.yaml`, `core/application/extensions/scaffold.py` | `Current` | `flow-steward extensions create <extension_id>` + scaffold regression tests | backend/extensions | local |
| Extension runtime, descriptor, snapshots, and projections share one atomic install lifecycle | `extension-manifest-standard.yaml`, ADR 0015 | `Current` | neutral lifecycle transaction and rollback tests in `tests/unit/test_extension_format.py` and `tests/unit/test_extension_api.py` | backend/extensions | PR + local |
| Descriptor artifact refs are bounded bundle-local documents with matching IDs | `extension-manifest-standard.yaml`, ADR 0015 | `Current` | descriptor validation/materialization tests in `tests/unit/test_extension_format.py` | backend/extensions/security | PR + local |
| Extension bundles contain no embedded database or private durable persistence | `extension-manifest-standard.yaml`, ADR 0015 | `Current` | extension boundary validator + `tests/unit/test_validate_extensions.py` | backend/extensions/security | PR + local |
| Extension archives declare and match the canonical runtime target | `extension-manifest-standard.yaml`, ADR 0015 | `Current` | bundle/installer compatibility tests and monitoring contract tests | backend/extensions | PR + local |
| Extension uninstall uses a revisioned impact plan and preserves foreign dependents | ADR 0015 | `Current` | backend lifecycle tests plus extensions API/UI modal tests | backend/frontend | PR + local |
| Extension data-schema upgrades run as staged, host-mediated, non-secret JSON transformations and apply atomically after revision recheck | `extension-manifest-standard.yaml`, ADR 0016 | `Current` | neutral migration-chain, SDK-envelope, rollback, and PostgreSQL lifecycle tests | backend/extensions/security | PR + local |
| No hardcoded secrets | `security-standard.md`, `configuration-standard.md` | `Current` | Bandit/Semgrep/detect-secrets + review | security | PR + local |
| Security headers on HTML surfaces | `security-standard.md` | `Current` | middleware + `tests/security/test_xss_rendering_contracts.py` | backend/security | PR + local |
| Runtime settings loaded through config modules | `configuration-standard.md` | `Current` | review; no dedicated automated config-placement gate yet | backend | PR |
| Project-specific mutable config does not live in `.env` | `configuration-standard.md` | `Current` | review + ADR exception path | backend/platform | PR |
| Config placement review | `configuration-standard.md` | `Current` | reviewer check only | backend/platform | PR |
| Generated artifacts use canonical supporting directories | `configuration-standard.md` | `Planned` | review only today; no artifact-placement gate yet | backend/platform | planned |
| New architectural exceptions require ADR | `architecture-standard.md` | `Current` | ADR + review | architecture | PR |
| UI-critical JS paths free of console/page errors | `frontend-standard.md`, `testing-standard.md` | `Current` | UI tests that fail on console/page errors for key flows | frontend | PR + local |
| Reusable UI behavior uses shared primitives | `frontend-standard.md` | `Current` | review | frontend | PR |
| Preserve unrelated dirty worktree changes | `agent-workflow-standard.md` | `Current` | workflow rule + review | contributors/agents | local |
| Destructive operations require explicit approval | `agent-workflow-standard.md` | `Current` | workflow rule | contributors/agents | local |
| CI exclusions are temporary debt, not permanent policy | `testing-standard.md` | `Current` | CI policy + review | engineering | PR + local |

## Review of Coverage

- `MUST` add an enforcement path for every new `MUST` rule.
- `SHOULD` prefer automated enforcement over manual review when practical.
- `SHOULD` keep the matrix updated when quality gates or architecture tests change.

## Near-Term Enforcement Priorities

The first enforcement improvements should focus on:

- naming guardrails
- architecture boundary tests
- bugfix regression requirements
- security boundary verification
- UI runtime-error detection on critical flows
- CI exclusion reduction
- duration-balanced fail-fast shards with independent matrix completion
- single-owner coverage collection per full gate
- automated validation of completion-evidence provenance
