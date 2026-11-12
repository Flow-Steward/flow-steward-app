# CLI and terminal UI contract

This is the current normative interface contract for
[ADR 0017](../architecture/adr/0017-schema-driven-cli-tui-stack.md).
Operational examples belong in the [guide](../GUIDE.md) and
[deployment runbook](../operations/compact-scalable-deployment.md).

## Ownership and runtime

`./flow-steward` is the host shell launcher. Python execution belongs to the
installation's Docker runtime, not a host interpreter. Scalable uses its retained
CLI container; Compact dispatches supported operations through its platform
runtime or product container. Command specifications declare Compact support;
unsupported operations must not be advertised as interchangeable between profiles.
Provider-console operations are documented separately in the deployment runbook.

Click owns parsing, prompt-toolkit the guided questions, Rich execution rendering and Pydantic
input/result validation. Dependency versions come from the repository dependency
locks, not a second manually maintained version table.

Application specifications live under
`core/application/management/command_specs/`; transport and presentation live
under `core/entrypoints/cli/platform/`. The closed `PUBLIC_COMMAND_SPECS` registry
is the command inventory. Each public path has one or two segments; adding a
command requires an application service and a declared specification, not an
opaque parser passthrough. There is no public task-submission command.

`CommandSpec` declares the path, identity, summary, examples, safety classification,
interactive policy, fields, input/result models, registered service, plan,
confirmation and result-renderer references. Safety classes are `read`, `change`
and `destructive`; interaction policies are `immediate`, `wizard_when_incomplete`
and `always_review`. Registered references are closed; configuration cannot
provide arbitrary imports or executable callbacks.

`FieldSpec` declares field identity, type, required/optional status, choices or a
bounded application choice provider, defaults, visibility, secret handling and
option/positional metadata. Domain validators remain application-owned. Shared
renderers must not create competing domain validation or bespoke command forms.

## Invocation and parameters

Line-oriented interaction with coloured, human-readable output is the default
for a terminal. Missing input is asked one question at a time; a choice is picked
with the arrow keys and Enter in a terminal and by number elsewhere. `--plain`
prints the same output without colour or animation. JSON and non-interactive
modes never prompt.
`CI` alone does not change semantics; actual TTY state and explicit flags do.

`--json` selects JSON output without color or prompts; it is the only mode that
prints JSON. `--no-color`, `NO_COLOR`, `TERM=dumb` and redirected stdout disable
color; plain output is understandable without animation.
Doctor remains line-oriented. Questions and output work at any terminal size.

Parameter availability is level-specific:

| CLI level | Available parameters |
| --- | --- |
| Root (`flow-steward`) | `-h`, `--help`, `--version`; `--json`, `--plain`, `--no-color`, `--verbose`, `--non-interactive` |
| Command group (`flow-steward users`) | `-h`, `--help`; `--json`, `--plain`, `--no-color`, `--verbose`, `--non-interactive` |
| Leaf command (`flow-steward users create`) | `-h`, `--help`; presentation/runtime flags and declared command inputs; `--input-json PATH` where supported by the command specification |

`--version` is root-only. `--input-json PATH` is leaf-only and supplies non-secret
structured input, not an output mode. Unknown fields fail validation.
`--input-json -` reads stdin and conflicts with a secret field also using stdin.
The supported presentation/runtime flags behave identically at every command depth.
They work before or after the command path; pre-scanning them must not reinterpret
command values or weaken parsing.

Input precedence is explicit positional/option input, input JSON, permitted
persisted non-secret configuration, specification default, then interactive input.
Review identifies inherited/default values. Validation completes before mutation;
conditional visibility never bypasses the normalized input model.

## Questions, review and secrets

Shared questions show the field label, its help and its default; required fields
are asked, optional ones only where the command asks for them. Choices are picked
from a list (arrow keys and Enter in a terminal, a number elsewhere) rather than
typed as enum keys. Keyboard only; nothing needs the mouse.

Review describes changes, unchanged/default values, effects, preservation steps
and warnings. The command's confirmation policy controls mutation; destructive
operations cannot be approved by an accidental generic Enter.

Secrets enter through masked prompts, declared file/stdin inputs or existing
secret references. Raw secrets are not ordinary CLI options, positional values
or input JSON. Review renders only configured/reference state. Passwords, tokens,
credential-bearing URLs and authorization values are redacted from diagnostic
output, journals and logs. One-time credential display is governed by the owning
command and is never repeated in logs.

## Output and errors

Shared execution rendering shows step state, elapsed time, quiet-work heartbeat,
safe technical output, remediation and log location. Redirected output is stable
line-oriented output; terminal animation is not an automation protocol.

Human results share one layout: a change starts with `✓`, `!` or `✗` and its
outcome, then aligned facts; lists of records are borderless tables; nested
values are indented sections; next steps and warnings come last. A command shown
to the operator (hints, next steps, help examples) is rewritten to the name the
launcher was run as, so it runs when pasted: `./flow-steward` or a path to it on
a host, `flow-steward` in the Compact image on a platform.

JSON mode emits one UTF-8 document on stdout with schema `flow-steward.cli.v1`,
`ok`, `command`, `operation_id` and `log_path`. Success adds `result` and `warnings`
(objects with `code` and `message`). Failure adds `error` with `code`, `message`,
`field_issues`, `hint`, `retryable` and `details`. Progress is not mixed into JSON
stdout. Launcher failures that cannot produce this envelope use stderr.

| Exit | Meaning |
| ---: | --- |
| 0 | Success, including warnings |
| 2 | Syntax or unknown option |
| 3 | Input validation |
| 4 | Unmet precondition |
| 5 | Permission/authentication |
| 6 | Conflict or active operation lock |
| 7 | Dependency unavailable |
| 8 | Failed operation with confirmed prior stable state |
| 9 | Recovery required or ambiguous resulting state |
| 10 | Operator interruption |
| 70 | Unexpected internal error |

Errors map through the shared registry, not per-command exit codes. The separate
provider-runtime dispatcher has its documented command-specific diagnostics.

## Lifecycle safety and verification

Mutating lifecycle operations use installation-owned locks and journals. A stale
lock requires explicit recovery; interruption never means destructive rollback.
Read-only diagnostics remain available where the transition can be observed
safely. CLI presentation changes never reset durable data, secrets or volumes.

Specification/Click tests own registry uniqueness, level-specific flags, generated
help, aggregate validation and exit mapping. Prompt tests own choice selection,
review and secret-safe rendering. Renderer/launcher tests
own JSON/plain/TTY behavior, signals, progress, logs and lock handling. Verification
follows the [testing standard](../standards/testing-standard.md), inside Docker.
