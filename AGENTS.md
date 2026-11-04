# Flow Steward AI Orchestrator

## Canonical Engineering Standards

`AGENTS.md` is the operational entrypoint for agents working in this repository.
Canonical engineering, architecture, testing, security, frontend, configuration,
and agent workflow rules live in `docs/standards/*`.

Rule precedence for repository work:

1. System and tool safety rules
2. Direct task-specific instructions
3. This `AGENTS.md`
4. `docs/standards/README.md`
5. Other files in `docs/standards/*`
6. ADRs in `docs/architecture/adr/*`
7. Supporting architecture and testing research docs when they do not conflict with the sources above

Source-of-truth policy:

- Update shared engineering rules in `docs/standards/*`.
- Do not maintain separate manual rule sets for specific AI tools.
- Record approved architectural exceptions or new architectural patterns in ADRs.
- Treat existing architecture/testing research docs as supporting references, not as competing rule sets.

## Target Repository Structure

Flow Steward is converging to one clean-slate repository layout for first release.

Target layout:

- `core/entrypoints/` — transport/controller layer only:
  - `admin/`
  - `api/`
  - `web/`
  - `ws/`
  - `cli/`
  - `mcp/`
- `core/application/` — first-party application logic:
  - domain use cases
  - services
  - repositories
  - models
  - schemas
  - query/policy/error modules
- `core/infrastructure/` — shared technical infrastructure only:
  - config
  - db helpers/pool/transactions
  - mail
  - observability
  - security
  - extension runtime support
- `extensions/` — external or self-hosted extensions only
- `db/schema/` — canonical SQL schema
- `db/seeds/` — canonical seed data
- `assets/` — JS, CSS, images, and frontend static assets
- `docs/`, `tests/`, `infra/`, `logs/`, `backups/`, `locales/` — supporting repository concerns (`core/entrypoints/cli/scripts/` hosts ops/CI shell wrappers)

Rules:

- New first-party code `MUST` follow the target layout above when creating or moving modules.
- Folders outside the target layout are transition-only and `MUST NOT` receive new permanent code unless the task is explicitly moving code out of them.
- `core/entrypoints` are controllers/adapters and `MUST NOT` contain business logic, persistence logic, or long-running orchestration logic.
- `core/application` owns first-party business logic and domain-oriented repositories.
- `core/infrastructure` owns shared technical adapters and runtime support, not business rules.
- `extensions/` `MUST NOT` be used as a home for first-party application code.
- Root `templates/` `MUST NOT` exist. Product/admin UI belongs in React; non-UI templates must live inside the package that owns them.

Operational setup, Docker workflows, monitoring endpoints, and extended command
references belong in `README.md` and `docs/GUIDE.md`, not here.

## Database Artifacts

The canonical end-state database artifact homes are:

- `db/schema/` — canonical SQL schema; this is the 1.0 baseline
- `db/seeds/` — canonical seed data
- `db/migrations/` — SQL migrations (`YYYYMMDDHHMMSS_slug.sql`) for changes after a released baseline

There are no historical migrations: the final 1.0 structure lives in
`db/schema/`. Installed databases never re-read `db/schema/`, so once a baseline has
shipped (the 0.9.x line included) a schema change `MUST` also add an idempotent
migration that makes the same change, and update `baseline_schema_fingerprint` in
`db/released_migrations.json` (ADR 0012, 2026-11-01 amendment).
Post-1.0 database changes `MUST` be SQL files under `db/migrations/` (no
per-migration Python files), applied by `flow-steward database upgrade --maintenance-confirmed --confirm` and tracked in
`public.platform_schema_migrations`. A root-level `migrations` directory stays
forbidden, and there is still no second DB source of truth (see ADR 0012).

## Tiered Quality Gates (MANDATORY)

The canonical selection, failure, retry, and evidence rules live in
`docs/standards/testing-standard.md` and `docs/standards/agent-workflow-standard.md`.

After each coherent edit batch and before Tier 1 tests, format only the files you
changed and inspect the diff:

```bash
python -m ruff check --fix <changed-python-files>
python -m ruff format <changed-python-files>

cd assets/frontend
npx --no-install eslint --fix <changed-ts-or-tsx-files>
```

Do not use repository-wide formatting for a scoped change and do not stage
formatter output automatically.

For ordinary work, including release preparation, select checks from the diff
and known dependencies, then run the regression and related tests fail-fast.
Use the accepted candidate SHA as the base and add concrete test paths/node IDs
for dependencies the import graph cannot see:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks targeted-tests --test-paths "auto:<accepted-sha> tests/unit/test_regression.py::test_case"
./core/entrypoints/cli/scripts/lint.sh --checks frontend-targeted-tests --test-paths "src/path/owning.test.tsx src/path/consumer.test.tsx"
```

The frontend wrapper accepts either `auto:<ref>` or an explicit list, not both
in one selection. Select affected Tier 2 checks by ID with `--checks`; their
existing check-level input scopes and fingerprint reuse remain unchanged.
This release's bare `lint.sh`, `--fast`, and `--full` select complete registry
scopes; use explicit `--checks` commands for ordinary work.

Full, heavy, nightly and Sonar runs require a separate explicit request.
Handoff, merge and publication never authorize them implicitly. GitHub quality
execution must be requested manually; a push or release is not such a request.
After failure or timeout, retain the log and candidate SHA and run only failed
or timed-out targets plus new impact against that SHA. Report unknown and unrun
coverage; do not restart the full gate automatically. See the canonical standards
for selection, retry and evidence requirements.

Tier 1 focused fix cycles stay narrow and fail-fast. The Tier 3 backend unit
suite, when explicitly requested, runs without `-x` or `--maxfail=1`, retains
the complete failure list, and reports failures grouped by root cause before a
scoped retry.

AC17-R97: Every tier runs inside the disposable `fs-audit` container, through `lint.sh --checks …`. Tier 1 is no exception: being focused does not mean running on the host. A host interpreter makes the result depend on the machine — Tier 1 could not run at all where `python3` is 3.14, and a host run of a suite passed while the same suite failed in the gate, because the gate substitutes the tree through `run-core-only-tests.sh`. `infra/tooling/quality/check-registry.json` is the single list of commands, and `tests/unit/tooling/quality/test_check_registry.py` fails if any check escapes the container.

All tiers use Docker with one registry owner per check. Local execution uses
`fs-audit`; separately requested hosted checks must preserve the selected scope.
Recheck main before merge; an updated candidate requires verification of its new
impact and matching evidence.
Reuse is fingerprint-bound; input-scoped Tier 2 checks reuse a pass across commits
and worktrees through the `flow-steward-quality-cache` volume (ADR 0036). Final handoff and release evidence must describe the
final commit SHA, selected checks, achieved coverage and pending work. A targeted
pass is not a full pass. An explicitly requested `--full` still validates
cumulative Tier 2 and Tier 3 evidence; publication does not request that run.
Public results never verify a different private commit. Additional audit checks
remain available through `--checks`.

For a focused fix/verify cycle, selected checks remain available:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks semgrep
./core/entrypoints/cli/scripts/lint.sh --checks pip-audit,bandit
```

Install hooks once per clone with `pre-commit install --hook-type pre-commit`.
The hook runs affected Tier 1 checks. Choose additional verification according to
the canonical standards; all local test tiers use the on-demand `fs-audit`
container.
