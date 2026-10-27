# ADR 0017: Schema-Driven CLI and Terminal UI Stack

## Status

Accepted

- Status note (2026-10-26): item 6 now says which runtime serves which commands
  on Scalable, matching item 7 and the launcher.

## Context

The first public release needs one discoverable management CLI, guided forms for
commands with several inputs, automation-safe non-interactive execution, stable
machine-readable output, visible progress, and actionable errors.

The predecessor command surface mixed nested `argparse` parsers, shell wrappers,
passthrough arguments, and command-specific prompting. Leaving the terminal
framework open to developer choice would permit multiple incompatible sources
of validation, help, prompting, output, and error behavior.

The public CLI must also run without host Python. Its presentation layer cannot
become a second home for application business logic.

## Decision

1. Public command and field contracts are first-party Pydantic models. The same
   specifications drive parsing, help, guided forms, validation, review,
   non-interactive execution, and JSON output.
2. Click owns the public command tree, option/argument parsing, shell-friendly
   help, and invocation context.
3. Textual owns the explicitly requested `--tui` full-screen guided forms,
   selectors, inline validation, review screens, keyboard interaction, and
   terminal UI tests.
4. Rich owns `--tui` post-confirmation progress, warnings, results, and
   structured error rendering in normal terminal history.
5. The first-party plain renderer is the default for both the CLI and installer.
   `--tui` opts in to the existing rich interface; `--plain` overrides it.
   The plain and JSON renderers use the same specifications and application
   command services. Non-TTY operation never opens Textual or prompts implicitly.
6. The public `./flow-steward` entrypoint dispatches to the installation's
   Docker-managed runtime and does not require a globally installed Python
   package. Scalable runs lifecycle commands in a versioned CLI image
   (`fs-cli`) and product and extension commands in the application runtimes
   (item 7); Compact dispatches supported commands through its platform/product
   runtimes as defined by ADR 0029 and the command specifications'
   `compact_support` metadata.
7. On Scalable, the CLI image runs as one private persistent `fs-cli` runtime with
   `restart: always` and its existing Docker socket access. The installer
   creates and verifies it; the launcher verifies and repairs its exact runtime
   identity when needed; public lifecycle commands exclude it; and every user
   command starts a fresh Python Click process: lifecycle commands run
   `core.entrypoints.cli.lifecycle_click_app` in `fs-cli`; product and
   extension commands run `core.entrypoints.cli.click_app` in the application
   (`fs-app`) or `fs-extension-manager` runtime.
8. Presentation adapters remain under `core/entrypoints/cli/`. Command
   specifications and application services remain under `core/application/`.
   Business logic, persistence, and lifecycle orchestration do not move into
   Click callbacks, Textual widgets, or Rich renderers.
9. Typer, Questionary, InquirerPy, `rich-click`, command-specific
   `input()`/`prompt()`, hand-written ANSI control, and a second public
   `argparse` command tree are not parallel first-release frameworks.
10. Exact dependency versions and hashes are locked for the CLI runtime. Pydantic
   uses the repository runtime pin rather than introducing a second version.

The normative interaction, renderer, automation, error, and command-inventory
contract is defined in [the CLI contract](../../contracts/cli.md).

## Consequences

- Required fields, allowed values, examples, validation, and automation cannot
  drift between command renderings.
- A developer has explicit library boundaries and cannot select a different
  framework per command.
- Guided forms can be visually rich without sacrificing scripts, CI, redirected
  output, or accessibility-oriented plain output.
- Textual is not kept open during long operations; Rich progress remains in
  normal terminal history and behaves predictably with signals and logs.
- The CLI image adds a small dedicated dependency surface that must be locked,
  scanned, tested on supported terminals, and upgraded deliberately.
- Existing commands require incremental adaptation to application services;
  parser callbacks are not copied wholesale into the new presentation layer.
