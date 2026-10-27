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
- `db/migrations/` — post-1.0 SQL migrations (`YYYYMMDDHHMMSS_slug.sql`); empty at 1.0

There are no historical migrations: the final 1.0 structure lives in
`db/schema/`. Post-1.0 database changes `MUST` be SQL files under `db/migrations/` (no
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

During a fix cycle, run the regression and impacted tests with fail-fast behavior.
`auto` derives the impacted tests from the diff against `main` (`auto:<ref>` for
another base); add what the import graph cannot see by path:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks targeted-tests --test-paths "auto tests/unit/test_regression.py"
./core/entrypoints/cli/scripts/lint.sh --checks frontend-targeted-tests --test-paths auto
```

Before an intermediate commit, run:

```bash
./core/entrypoints/cli/scripts/lint.sh --fast
```

Before handing off, opening a pull request, or merging a coherent change batch,
run one clean full gate from the final committed state:

```bash
./core/entrypoints/cli/scripts/lint.sh --full
```

Tier 1 focused fix cycles stay narrow and fail-fast. The Tier 3 backend unit
suite runs without `-x` or `--maxfail=1`, retains the complete failure list,
and reports failures grouped by root cause so one coherent in-scope batch can
be fixed before the next full run.

AC17-R97: Every tier runs inside the disposable `fs-audit` container, through `lint.sh --checks …`. Tier 1 is no exception: being focused does not mean running on the host. A host interpreter makes the result depend on the machine — Tier 1 could not run at all where `python3` is 3.14, and a host run of a suite passed while the same suite failed in the gate, because the gate substitutes the tree through `run-core-only-tests.sh`. `infra/tooling/quality/check-registry.json` is the single list of commands, and `tests/unit/tooling/quality/test_check_registry.py` fails if any check escapes the container.

Quality tiers 1–3 are local Docker gates with one registry owner per check.
Reuse is fingerprint-bound; input-scoped Tier 2 checks reuse a pass across commits
and worktrees through the `flow-steward-quality-cache` volume (ADR 0036). Final handoff and release evidence must describe the
final commit SHA. Candidate publication requires complete green Tier 2 and Tier 3
evidence for that same SHA; `--full` refreshes or reuses exact-SHA Tier 2 before
the Tier 3 delta and succeeds only after cumulative validation. GitHub
Actions are disabled; no CI artifact or remote check is
required. Additional audit checks remain available through `--checks`.

For a focused fix/verify cycle, selected checks remain available:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks semgrep
./core/entrypoints/cli/scripts/lint.sh --checks pip-audit,bandit
```

Install hooks once per clone with `pre-commit install --hook-type pre-commit`.
The hook runs the fast gate. Full gates and SonarQube run locally in Docker;
all modes use the on-demand `fs-audit` container by default.
