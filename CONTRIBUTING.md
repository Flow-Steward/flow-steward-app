# Contributing to Flow Steward

Thanks for helping improve Flow Steward.

By contributing to this repository, you agree that your contributions are
submitted under the repository license (Apache License 2.0).

## Report a bug or request a feature

1. Search the [existing issues](https://github.com/Flow-Steward/flow-steward-app/issues)
   first, open and closed.
2. Open a new issue with the version, steps to reproduce, expected and actual
   result, and the job id or screenshots when they help.
3. For a bug, attach the System Diagnostics report: **System → Monitoring →
   Download diagnostics** in the UI, or `./flow-steward reports diagnostics` on
   the server. It holds no logs, job content or secret values; glance through
   it before attaching. Never paste passwords, tokens or customer data.

Suspected vulnerabilities are never public issues: follow [SECURITY.md](SECURITY.md).

## Change the code

1. **Issue first.** Agree on the approach in an issue before larger changes
   (new features, contracts, database changes, dependencies, architecture).
2. **Fork and branch** from the latest `main`; one change per pull request.
3. **Read the rules**: [AGENTS.md](AGENTS.md) and
   [docs/standards/](docs/standards/README.md) apply to people and AI agents.
4. **Work in the Development environment**: `./install.sh --environment development`.
   Docker is the only requirement; tests never run on the host.
5. **Bug fix: write the regression test first** and see it fail.
6. **Verify in tiers**, all inside Docker:
   - while fixing: `./core/entrypoints/cli/scripts/lint.sh --checks targeted-tests --test-paths auto`
     (and `--checks frontend-targeted-tests --test-paths auto` for frontend changes);
   - before each commit: `./core/entrypoints/cli/scripts/lint.sh --fast`
     (install the hook once with `pre-commit install --hook-type pre-commit`);
   - before the pull request, from the final commit: `./core/entrypoints/cli/scripts/lint.sh --full`.
7. **Open the pull request** against `main` with `Fixes #<issue>`, what changed,
   and the evidence: exact commands, exit codes and the commit SHA the full gate
   ran on. GitHub Actions are disabled, so this local evidence is what reviewers
   see.
8. **Review loop**: answer every comment, push fixes to the same branch, and run
   `--full` again on the final commit. A maintainer merges.

Update documentation in the same pull request when behavior, commands, APIs or
settings change.

## Extensions and workflow templates

Extensions and workflow templates are not added to this repository. Publish
them to the public catalogs:
[Flow-Steward/extensions](https://github.com/Flow-Steward/extensions) and
[Flow-Steward/workflows](https://github.com/Flow-Steward/workflows); each
README explains the submission.

## AI agents

The Flow Steward agent plugin in [docs/agent-skills/flow-steward](docs/agent-skills/flow-steward/README.md)
has skills for this whole process: `flow-steward-report-issue`,
`flow-steward-contribute-code`, `flow-steward-publish-extension` and
`flow-steward-publish-workflow`.

## Code of Conduct

All participants are expected to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
