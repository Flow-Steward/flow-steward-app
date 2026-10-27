# Configuration Standard

This document defines how configuration values are classified, stored, loaded, and documented in the Flow Steward repository.

## Purpose

The goal of this standard is to keep constants, runtime settings, secrets, and project-specific configuration consistent and easy to reason about.

## Configuration Classes

The repository uses four configuration classes:

1. Code constants
2. Runtime settings
3. Project-specific mutable configuration
4. Operator documentation and example files

## Code Constants

Code constants are stable values that express code invariants, protocol names,
fixed defaults, patterns, and internal sets.

Rules:

- `MUST` keep code constants in the owning module or subsystem config module.
- `MUST` use code constants for invariant values that do not vary by environment.
- `SHOULD` group related constants by subsystem.
- `SHOULD` give constants descriptive names and keep them near the code that uses them.

Examples:

- public path sets
- regex patterns
- default redact key lists
- role name sets
- internal protocol strings

## Runtime Settings

Runtime settings are values that vary by environment, deployment, infrastructure,
or operator policy.

Rules:

- `MUST` store runtime settings in environment variables or sanctioned secrets/config providers.
- `MUST` load runtime settings through config modules rather than ad hoc `os.getenv()` reads across feature code.
- `MUST` keep defaults explicit in the config layer.
- `SHOULD` use clear prefixes and stable naming for environment variables.
- `SHOULD` keep parsing and normalization logic in config helpers.

Examples:

- DSNs
- API keys
- base URLs
- ports
- feature flags
- log levels
- retention periods
- timeout knobs

## Project-Specific Mutable Configuration

Project-specific mutable configuration is data that differs between projects at runtime
and belongs to project records, policies, or stored config.

Filesystem path values can cross this boundary:

- operator-provided bootstrap or mount paths are runtime settings
- persisted per-project metadata belongs in project records once the platform stores it there

Rules:

- `MUST` store per-project mutable configuration in the project data model, not in `.env`.
- `MUST` treat project configuration as runtime data, not code constants.
- `SHOULD` keep project config retrieval behind the owning service or runtime path.
- `SHOULD` document project-level config contracts when they affect operators or agents.

Examples:

- workflow bindings
- project policies
- per-project agent settings
- project-scoped connection settings
- persisted project metadata and policies

## Secrets

- `MUST` keep secrets out of source code, templates, fixtures, and example env files.
- `MUST` load secrets from environment variables or approved secret storage paths.
- `MUST` redact secrets from logs, debug payloads, and error output.
- `SHOULD` keep secret names explicit and operator-friendly.
- `SHOULD` separate secret values from non-secret feature toggles when practical.

## Config Module Policy

- `MUST` define shared runtime settings in canonical infrastructure config modules (for example `core/infrastructure/config/*`) or subsystem-specific config modules.
- `core/config.py` is a support-only import surface and `MUST NOT` be the canonical home for new configuration logic.
- `MUST` keep parsing, normalization, and defaults in the config layer.
- `MUST` avoid spreading duplicated env parsing logic through routes, services, and templates.
- `SHOULD` extract subsystem-specific settings into subsystem config modules when the setting set grows.
- `SHOULD` keep config structures readable and grouped by concern.

## Environment File Policy

Canonical environment documentation lives in:

- `.env.example`
- `.env.example.prod`
- `.env.example.test`
- `.env.example.quality`
- `.env.example.ops`
- `README.md`
- `docs/GUIDE.md`
- `docs/user/configuration-reference.md`
- `docs/configuration/flow-steward-env-catalog.md`
- `docs/configuration/flow-steward-env-audit.json`

Rules:

- `MUST` treat `.env.example*` files as operator documentation and bootstrap aids.
- `MUST` keep example files aligned with the real supported variables.
- `MUST` avoid undocumented environment variables on production paths.
- `MUST` regenerate `docs/configuration/flow-steward-env-catalog.md` after changing
  `.env.example*`, the env manifest, or env-read surfaces that affect the catalog.
- `SHOULD` keep quickstart env files minimal and move advanced knobs to overlays.
- `SHOULD` document the purpose of each env file and overlay.

## Supporting Repository Directories

Rules:

- `MUST` use `infra/` for static deployment and environment manifests only.
- `MUST` use `logs/` for generated operational artifacts such as runtime logs, audits, coverage exports, scanner output, and health snapshots.
- `MUST` use `backups/` for encrypted backups and restore drill artifacts only.
- `MUST NOT` use `infra/`, `logs/`, or `backups/` as homes for application code.
- `SHOULD` keep generated artifacts out of `core/`, `assets/`, and `docs/`.

## Choosing the Right Home for a Value

- `MUST` keep a value in code when it is a code invariant.
- `MUST` keep a value in env-backed config when it varies by environment, deployment, secret, or operator policy.
- `MUST` keep a value in project config when it varies by project at runtime.
- `MUST` treat deployment-local bootstrap paths and mount points as runtime settings unless the platform intentionally persists them as project data.
- `SHOULD` document the chosen home when a value could plausibly belong in more than one layer.

**Example — config placement:**

```python
# Code constant in the owning module
DEFAULT_PAGE_SIZE = 20

# Runtime setting in a config module
AUTH_LOGIN_MAX_ATTEMPTS = int(os.getenv("FS_AUTH_LOGIN_MAX_ATTEMPTS", "5"))

# Runtime setting in a config module
FS_PUBLIC_BASE_URL = os.getenv("FS_PUBLIC_BASE_URL", "https://localhost:8099")

# Project-specific mutable config stays in project data, not in .env
project_policy = project_record["policy"]
```

## Review Expectations

- `MUST` reject new hardcoded secrets.
- `MUST` reject feature code that parses env vars ad hoc when a config module is the correct home.
- `MUST` reject project-specific mutable settings placed in `.env` without a strong architectural reason.
- `MUST` reject generated backup, report, or log artifacts committed outside their canonical supporting directories without a strong reason.
- `SHOULD` prefer one clear config source per setting instead of layered ambiguity.
