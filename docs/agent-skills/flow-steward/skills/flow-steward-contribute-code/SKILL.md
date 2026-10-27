---
name: flow-steward-contribute-code
description: Use when a contributor wants to fix a bug or propose an improvement in the Flow Steward core repository Flow-Steward/flow-steward-app; covers the issue, fork and branch, the Development environment, the regression test, the lint.sh quality tiers, commits, the pull request with evidence and the review loop.
---

# Contribute Code to Flow Steward

The core repository is <https://github.com/Flow-Steward/flow-steward-app>
(Apache-2.0). Issues and pull requests go there. Extensions and workflow
templates are published elsewhere: use `flow-steward-publish-extension` or
`flow-steward-publish-workflow` for those.

The repository's own rules win over this skill. Read, in this order:
`AGENTS.md`, `docs/standards/README.md`, `docs/standards/agent-workflow-standard.md`,
`docs/standards/testing-standard.md`, then the standard for the area you touch
(`architecture-standard.md`, `backend-python-standard.md`,
`frontend-standard.md`, `security-standard.md`, `configuration-standard.md`).
Security vulnerabilities go privately to `security@flow-steward.org`
(`SECURITY.md`), never to a public issue or pull request.

## 1. Start from an issue

Search the issues first (`gh issue list --repo Flow-Steward/flow-steward-app --state all --search "<words>"`).
If none exists, open one (the `flow-steward-report-issue` skill) and describe
the problem and the change you propose. For anything larger than a small bug
fix — a new feature, a changed contract, a database change, a new dependency,
a new architectural pattern — wait for a maintainer to agree on the approach
before writing code. One issue, one pull request, one change.

## 2. Fork, clone and branch

```bash
gh repo fork Flow-Steward/flow-steward-app --clone
cd flow-steward-app
git switch -c fix/<issue-number>-<short-slug>
pre-commit install --hook-type pre-commit
```

Use a full clone, not a shallow one. Keep the branch on top of the latest
`main` (`git fetch upstream && git rebase upstream/main`).

## 3. Development environment

Work in the **Development** environment, never on a production installation:

```bash
./install.sh --environment development --file-storage bundled
./flow-steward status
./flow-steward doctor
```

It runs the core plus the frontend and keeps the `fs-test-runner` and
`fs-audit` containers ready for tests and quality gates. Docker with Compose v2
runs the product and every test; never treat a test run with a host Python or
Node as evidence.
`./flow-steward environment show` tells which environment is installed. Set
`FS_AUTO_RELOAD=1` for backend reload, and start `fs-frontend` for Vite hot
reload (see the repository's `docs/GUIDE.md`).

## 4. Make the change

- Find the owning code and its tests first; read callers before changing a
  function.
- Put code where `AGENTS.md` says: `core/entrypoints/` only adapts transports,
  `core/application/` holds business logic, `core/infrastructure/` holds
  technical adapters. Frontend code lives in `assets/frontend/` and reuses the
  existing components.
- **Bug fix: write the regression test first** and watch it fail for the
  expected reason before fixing.
- Database changes after 1.0 are SQL files in
  `db/migrations/YYYYMMDDHHMMSS_slug.sql`; `db/schema/` is the 1.0 baseline.
- Update docs in the same pull request when behavior, a command, an API or a
  setting changes. An architectural decision needs an ADR in
  `docs/architecture/adr/`.
- Keep unrelated cleanup out of the change.

After each edit batch, format only the files you changed:

```bash
python -m ruff check --fix <changed-python-files>
python -m ruff format <changed-python-files>
cd assets/frontend && npx --no-install eslint --fix <changed-ts-or-tsx-files>
```

## 5. Verify in tiers

Every tier runs in the disposable `fs-audit` container through
`./core/entrypoints/cli/scripts/lint.sh`.

| Tier | When | Command |
| --- | --- | --- |
| 1 | during the fix, fail-fast | `./core/entrypoints/cli/scripts/lint.sh --checks targeted-tests --test-paths "auto tests/unit/<regression_test>.py"` and, for frontend changes, `--checks frontend-targeted-tests --test-paths auto` |
| 2 | before each commit (the pre-commit hook runs it) | `./core/entrypoints/cli/scripts/lint.sh --fast` |
| 3 | once, from the final commit, before the pull request | `./core/entrypoints/cli/scripts/lint.sh --full` |

`auto` selects tests from your diff against `main`; add by path what the import
graph cannot see (runtime, database, extension tests). Diagnose a failure
before rerunning it. `--full` runs the whole backend suite without stopping at
the first failure: group the failures by root cause and fix them as one batch.
Never commit with `--no-verify`. GitHub Actions are disabled in this
repository, so your local gate result is the evidence reviewers see.

## 6. Commit

Commit when Tier 2 passes. Write a plain one-line summary of what changes for
the user or the code, for example
`Keep every lane's writes when parallel lanes finish together`, and a body
that says why. Reference the issue (`Refs #123`). Do not commit secrets,
`.env` files, logs, screenshots, task plans or audit reports.

## 7. Pull request

```bash
git push -u origin fix/<issue-number>-<short-slug>
gh pr create --repo Flow-Steward/flow-steward-app --base main --title "<summary>" --body-file pr.md
```

The description contains:

- `Fixes #<issue>` (or `Refs #<issue>` for partial work);
- what was wrong and what changed, in a few sentences;
- how it was verified: the exact commands, their exit codes, and the commit
  SHA `--full` ran on; the regression test name; for UI changes, before and
  after screenshots without private data;
- risk and anything not verified (for example: not tried on ARM64, not tried
  on Scalable).

## 8. Review loop

Answer every review comment, either with a change or with a reason. Push
fixes as new commits on the same branch; rerun Tier 1 for the touched area and
`--fast` before each commit, and `--full` again on the final commit before you
ask for another review. Update the description's evidence with the new SHA.
Do not merge your own pull request; a maintainer merges it.
