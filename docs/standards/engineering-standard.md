# Engineering Standard

This document defines the default engineering rules for the Flow Steward repository.
It applies to all contributors and all AI agents.

## Purpose

The goal of this standard is to keep naming, code structure, typing, error handling,
and code quality consistent across the repository.

## Naming and Terminology

- `MUST` use canonical project terminology.
- `MUST` introduce no new identifiers containing retired project codenames.
- `MUST` avoid retired terms in new code, APIs, tests, and docs.
- `SHOULD` prefer domain-descriptive names over technical or branded names.
- `SHOULD` rename low-risk local retired identifiers when touching a file.
- `MAY` defer broad retired-name cleanup when the blast radius is unrelated to the task.

## Code Shape and Modularity

- `MUST` keep one module focused on one primary responsibility.
- `MUST` extract new responsibility blocks when a file becomes a hotspot for unrelated concerns.
- `MUST` keep business rules out of transport adapters, glue code, and one-off scripts.
- `SHOULD` review files exceeding 300 lines for extraction opportunities.
- `SHOULD` treat files above roughly 500 lines or functions above roughly 50 lines as refactor triggers, unless the touched area is a documented out-of-layout hotspot and extraction would widen risk.
- `SHOULD` split modules by responsibility before they become difficult to review or test.
- `SHOULD` prefer adding a small focused module over extending a god file.
- `MAY` defer large structural cleanup in out-of-layout hotspots when the current task is narrow, but new logic should still land in the smallest responsible module.

## Duplication Policy

- `MUST` avoid duplicating business logic across route, UI, MCP, and service layers.
- `MUST` extract shared rules when the same behavior is implemented more than once on active code paths.
- `SHOULD` tolerate narrow duplication only when extraction would introduce worse coupling or premature abstraction.
- `SHOULD` duplicate tests only when each duplicate proves a distinct contract.

## Immediate Formatting

- `MUST` apply the repository's configured safe formatter to supported source files immediately after each coherent edit batch and before Tier 1 tests.

AC17-R97: Every tier runs inside the disposable `fs-audit` container, through `lint.sh --checks …`. Tier 1 is no exception: being focused does not mean running on the host. A host interpreter makes the result depend on the machine — Tier 1 could not run at all where `python3` is 3.14, and a host run of a suite passed while the same suite failed in the gate, because the gate substitutes the tree through `run-core-only-tests.sh`. `infra/tooling/quality/check-registry.json` is the single list of commands, and `tests/unit/tooling/quality/test_check_registry.py` fails if any check escapes the container.
- `MUST` pass only files changed by the contributor to edit-loop formatting commands.
- `MUST` inspect the resulting diff before testing or staging the files.
- `MUST NOT` run repository-wide formatting for an ordinary scoped change.
- `MUST NOT` enable unsafe automatic fixes or stage formatter output automatically.
- `MUST NOT` reformat generated, vendored, migration, or third-party files unless the task explicitly owns them and their canonical tooling permits it.

For changed Python files:

```bash
python -m ruff check --fix <changed-python-files>
python -m ruff format <changed-python-files>
```

For changed `.ts` and `.tsx` files below `assets/frontend`, run ESLint from that workspace with paths relative to it:

```bash
cd assets/frontend
npx --no-install eslint --fix <changed-ts-or-tsx-files>
```

If Ruff is unavailable on the host, run the same Ruff commands in the repository's
`fs-audit` environment. Other file types remain check-only until a canonical
formatter is adopted.

## Typing and Interfaces

- `MUST` add type hints to public functions, methods, and classes.
- `MUST` keep input and output contracts explicit.
- `MUST` prefer structured types and schemas over loosely shaped dictionaries.
- `SHOULD` narrow types at boundaries instead of passing untyped payloads deeper into the system.
- `SHOULD` make invalid states harder to represent through types and validation.

## Error Handling

- `MUST` catch specific exceptions when handling failures.
- `MUST` log enough context to debug a failure without leaking secrets.
- `MUST` either re-raise, map, or explicitly absorb an exception with a justified reason.
- `MUST` use the catch-specific → log → re-raise/map pattern on production paths.
- `SHOULD` translate low-level failures into domain-appropriate or transport-appropriate errors at boundaries.
- `SHOULD` keep retry, fallback, and degraded-mode behavior explicit.

**Example — error handling pattern:**

```python
# Correct: catch specific, log context, re-raise as domain error
try:
    result = await db.execute(query)
except IntegrityError as exc:
    logger.warning("duplicate_entry", project_id=project_id, detail=str(exc))
    raise DuplicateProjectError(project_id) from exc
except OperationalError as exc:
    logger.error("db_unavailable", detail=str(exc))
    raise ServiceUnavailableError("database") from exc
```

## Logging and Observability

- `MUST` log operationally relevant failures, retries, and state transitions at the correct layer.
- `MUST` keep logs structured and useful for incident review.
- `MUST` avoid logging secrets, tokens, raw credentials, or sensitive payloads.
- `SHOULD` include identifiers that help correlate runtime events, such as project, job, thread, or request context.

## Constants and Configuration

- `MUST` extract magic numbers, protocol strings, event names, and feature switches into named constants or config.
- `MUST` keep code invariants in the owning module or subsystem config module.
- `MUST` read environment-specific settings and secrets through sanctioned config paths.
- `SHOULD` keep defaults explicit and close to the config definition.
- `SHOULD` group related constants by domain or subsystem.

**Example — constants over magic numbers:**

```python
DEFAULT_PAGE_SIZE = 20
MAX_RETRY_ATTEMPTS = 3

for attempt in range(MAX_RETRY_ATTEMPTS):
    rows = fetch_rows(limit=DEFAULT_PAGE_SIZE)
```

## Comments and Documentation

- `MUST` keep comments accurate and remove them when they become misleading.
- `SHOULD` use comments to explain intent, invariants, or tricky behavior, not obvious syntax.
- `SHOULD` add or update docs when a change alters developer workflow, architecture, or a stable contract.
- `MAY` omit comments when the code is already clear from naming and structure.

## Workflow Builder Runtime Rules

- `MUST` use `Test Run` for testing inside the workflow builder runtime UX. The acknowledged ordinary `Run workflow` action is the explicit exception authorized by ADR 0032; it uses saved definitions and shared normal job views separately from Test Run sessions.
- `MUST NOT` add new builder-visible runtime terms or surfaces such as `Simulate`, `Dry Run`, `Preview run`, `Run Lab`, `Prompt Preview`, or `Contract Preview`.
- `MUST` treat builder testability as part of step-kind completeness for every shipping workflow step kind.
- `MUST NOT` introduce a new shipping step kind without builder editor support, input/output contracts, always-visible variable visibility, `Test Run`, structured result/error handling, and tests.

## Review Expectations

- `MUST` reject new retired-name reuse, new transport-level business logic, and unverified bug fixes.
- `MUST` treat large unstructured files, repeated logic, and weak assertions as quality issues.
- `SHOULD` prefer small, focused, verifiable changes over broad opportunistic rewrites.
