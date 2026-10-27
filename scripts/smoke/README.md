# Flow Steward release smoke gate

`release-smoke.sh` is the non-interactive gate for one immutable release candidate. The canonical
machine-readable inventory is `checks.json`.

The current manifest contains **69 retained semantic checks**: A=16, B=18, C=15, D=12, E=8.
The runner derives this denominator directly from `checks.json`; removed HTTP/JSON aliases are not
counted as tests and historical 72-check evidence is retained only in the remediation ledger.

## Run

```bash
./scripts/smoke/release-smoke.sh
./scripts/smoke/release-smoke.sh A C
./scripts/smoke/release-smoke.sh --profile release
```

The default `oss` profile is useful for focused iteration. The mandatory `release` profile includes
OSS, dormant-account, and deployment checks in one denominator and enables E09-E12:

```bash
FS_SMOKE_PROFILES=oss,tenant ./scripts/smoke/release-smoke.sh C
FS_SMOKE_PROFILES=oss,deploy FS_SMOKE_DEPLOY=1 ./scripts/smoke/release-smoke.sh E
./scripts/smoke/release-smoke.sh --profile release
```

The release profile requires explicit `FS_SMOKE_CANDIDATE_COMMIT`,
`FS_SMOKE_IMAGE_DIGESTS`, `FS_SMOKE_COMPOSE_PROJECT`, and `FS_SMOKE_APP_CONTAINER`. The runner
compares those inputs with both `/health` and Docker image metadata before any group can run.

Build and operate that candidate from durable repo-owned inputs (the generated non-secret files are
kept under the ignored `.flow-steward/release/<commit>/` directory):

```bash
./scripts/release/build_candidate.sh
FS_RELEASE_CANDIDATE_COMMIT="$(git rev-parse HEAD)" \
  ./scripts/release/candidate_prepare.sh database
FS_RELEASE_CANDIDATE_COMMIT="$(git rev-parse HEAD)" \
  ./scripts/release/candidate_prepare.sh worker-identity
FS_RELEASE_CANDIDATE_COMMIT="$(git rev-parse HEAD)" \
  ./scripts/release/candidate_stack.sh up -d --no-build
```

For publication, use the single wrapper instead of replaying those commands manually. It builds or
reuses one finalized candidate, prepares its isolated data plane and worker identity, starts the
runtime, derives every non-secret smoke input from the release directory and exact Compose project,
then runs the safe OSS/tenant profiles once:

```bash
FS_SMOKE_EMAIL=... FS_SMOKE_PASSWORD=... FS_SMOKE_BEARER_TOKEN=... \
FS_SMOKE_MODEL_PROVIDER=custom:<provider-id> FS_SMOKE_MODEL=luna \
  ./scripts/release/candidate_publish.sh
```

The final release decision remains explicit and runs the complete profile twice from the same
candidate identity. Deploy mutation is never enabled implicitly:

```bash
FS_RELEASE_CANDIDATE_COMMIT=<full-sha> FS_SMOKE_DEPLOY=1 \
FS_SMOKE_PROJECT_ID=<disposable-it-project-id> \
  ./scripts/release/candidate_verify.sh --release
```

`candidate_verify.sh` reads `candidate.env` and `candidate-provenance.json`, validates their exact
commit/digests, discovers singular running service containers only inside the derived Compose
project, and supplies the durable operation scripts. Protected credentials and the account-scoped
model route remain explicit inputs and fail before smoke mutation when absent. The GitHub release
workflow now accepts only the trusted Tier 3 run id; it uses this same candidate-owned derivation
instead of manually transcribed ports, containers, digests, or script paths.

The phase-one candidate harness calculates one secret-safe snapshot before the first image build,
allocates candidate-owned ports, and finalizes exact image tags/digests, extension content, and
provenance after publication. A rerun reuses the finalized candidate only when those inputs still
match and every recorded image digest remains available locally. Read-only inspection is available
through:

```bash
COMMIT="$(git rev-parse HEAD)"
python -m core.entrypoints.cli.ops.candidate_harness status --candidate "$COMMIT"
python -m core.entrypoints.cli.ops.candidate_harness snapshot \
  --candidate "$COMMIT" --source-env .env.prod --check
python -m core.entrypoints.cli.ops.candidate_harness preflight \
  --candidate "$COMMIT" --source-env .env.prod
```

Exact cleanup requires the full SHA and explicit confirmation. It removes only resource identities
recorded in the finalized snapshot whose Compose/revision ownership still matches; candidate state
is removed only after retained external evidence checksums verify:

```bash
python -m core.entrypoints.cli.ops.candidate_harness cleanup \
  --candidate "$COMMIT" --confirm
```

Phase one proves source/snapshot identity, static host prerequisites, phase timings/evidence, and
exact cleanup. It does **not** claim authenticated API/PAT/CLI/MCP/provider/model readiness or the
two complete release-smoke executions; those remain release-candidate verification steps.

`build_candidate.sh` refuses tracked or staged changes, reuses only an exact revalidated finalized
candidate, labels all three images with the exact commit, writes their image IDs to `candidate.env`,
and emits a provenance manifest containing the exact ledger/validator hashes and candidate ancestor
set. The two explicit preparation commands are
idempotent: the first starts only candidate-owned infrastructure and applies the canonical database
upgrade; the second provisions or validates the dedicated `projects.read` worker identity without
printing its token. Ordinary `candidate_stack.sh up` never creates credentials. It first validates
required files and secrets, digest provenance, Compose ownership, migration state, and the configured
worker credential, and fails before runtime-service mutation with these exact commands when a
precondition is missing. In a core-only runner without `.git`, pass the provenance manifest
explicitly:

```bash
python infra/tooling/release/validate_ledger.py \
  --provenance .flow-steward/release/<commit>/candidate-provenance.json
```

The runner prints the candidate commit, image/container identifiers, environment profiles, running
worker count, PASS/FAIL/SKIP totals, and cleanup result. Required SKIP or missing results are FAIL.
Exit status is the failure count, capped at 125 so it remains a portable shell status.
Each complete release invocation also records one UTC Docker log window and fails on unexpected
candidate-owned ERROR, foreign-key/FK, protocol, requeue, or traceback lines from `fs-app`,
`fs-worker`, `fs-mcp`, and `control-plane`. Any intentional signature must be listed as an exact
full line for one service in `candidate-log-allowlist.json`; substring and global allowlists are not
accepted.

## Safety prerequisites

- Exactly one running Compose `fs-worker` consumer must exist. The runner fails before invoking a
  group when topology is zero or ambiguous.
- Groups B, C, and D mutate data and require `FS_SMOKE_ISOLATED=1` plus an explicit
  `FS_SMOKE_PG_CONTAINER` for the disposable audit/candidate data plane. They fail closed on the
  shared product data plane.
- `FS_SMOKE_EMAIL` and `FS_SMOKE_PASSWORD` must be supplied through protected environment inputs.
  Do not put credentials in this directory.
- A19-A22 use the repo-owned `candidate_semantic_checks.py` runner by default. It exercises the
  authenticated packaged API and the explicitly named disposable candidate database. Protected
  automation may override an individual runner with the corresponding `FS_SMOKE_*_SCRIPT` input,
  but a missing override is never a reason to skip these required checks.
- A25 exercises the callback lifecycle of the synthetic `acme.*` probe through public
  project-connection and callback APIs, including redacted list output, HMAC delivery, invalid-signature and expiry
  denial, durable replay rejection, token rotation, old-token invalidation, and status dispatch.
  A26 creates its own workflow and agent (a release ships no extension to borrow them from), then a
  project-scoped guardrail; it PATCHes every Config field without sending `agents`, reloads it, and
  requires the original agent scope to remain exact. A27 uses public lifecycle operations around a project populated with a
  workflow, dataset, extension connection/state, and completed artifact job; it proves restore, stale-plan denial without partial mutation, clean retry,
  permanent deletion, and artifact cleanup.
- Candidate rebuild/restart/database upgrade/rollback checks are deploy-profile-only. E10-E12
  require durable executable operator scripts through `FS_SMOKE_ROLLBACK_SCRIPT`,
  `FS_SMOKE_UPGRADE_SCRIPT`, `FS_SMOKE_REBUILD_SCRIPT`, and `FS_SMOKE_RESTART_SCRIPT`; paths under `/tmp` and
  `/private/tmp` are rejected. E12 also requires a protected bearer token and a disposable
  project for the authenticated cutover run. The canonical values are
  `scripts/release/candidate_upgrade.sh`, `scripts/release/candidate_rollback.sh`, `scripts/release/candidate_rebuild.sh`, and
  `scripts/release/candidate_restart.sh`. They require the exact immutable commit and its derived
  `fs-candidate-<12-char-commit>` Compose project; rollback additionally verifies the explicitly
  named PostgreSQL container before running dump/restore.
- Normal cleanup uses product APIs. Direct SQL in legacy live checks is allowed only against the
  explicitly named isolated PostgreSQL container for exact evidence or emergency cleanup.

Groups A and E now probe the named packaged candidate rather than executing source-mounted test
references. E09-E12 execute the candidate upgrade, rollback/parity, rebuild/provenance, and
restart/authenticated-cutover operations. Groups B-D exercise candidate APIs/runtime behavior.
D05 accepts only `completed` and verifies its declared `weather_raw` effect; pending, approval,
timeout, cancellation, and failure never pass. B01 likewise dispatches a task on a plain project and
passes only on `completed`.

D09 and D10 own what the platform owns whichever bundles ship, and they run on a throwaway `acme.*`
probe the smoke installs through the public install endpoint — never the internal bootstrap. D09
covers projection lifecycle, public catalog state, a disabled extension's workflow being unavailable
and unrunnable, one canonical job phase, rejected authoring writes leaving nothing behind, saved-query
storage integrity against whichever connector the catalog offers, and cleanup after uninstall. D10 covers external-effect persistence and its statuses on the probe's own
action, and that an action cannot reach a sibling project's connection or another account.

A release ships no extensions; each lives in its own repository. A bundle's own assertions — its
workflows, its operations, the mail it sends — live in its `release/checks.py` and run under the
`BUNDLES` check for every bundle installed on the candidate, one isolated container per bundle. A
clean candidate has none, and `BUNDLES` records zero suites. See
`bundle_release_checks.py` for the implemented sandbox and
[release publication](../../docs/operations/official-release-publication.md) for
candidate requirements. Suite isolation does not imply least-privilege API
authorization: fixture DELETE requests still use broad administrative scope.

## Running `BUNDLES`

```
FS_SMOKE_CANDIDATE_INTERNAL_BASE   how the candidate answers on the suite network (http://fs-app:8099)
FS_SMOKE_CANDIDATE_APP_CONTAINER   the candidate's app container, connected to that network
FS_SMOKE_CANDIDATE_APP_ALIAS       the alias the URL above names (fs-app)
FS_SMOKE_RELEASE_SERVICE_CONTAINERS  alias=container pairs a suite may reach, e.g. mailpit=<container>
FS_SMOKE_EXTENSIONS_ROOT           where the bundles are, when not /app/extensions
```

Each suite additionally receives the inputs it declares in `RELEASE_INPUTS`, read from the runner's
environment. A suite that calls a model declares `FS_SMOKE_MODEL_PROVIDER` and `FS_SMOKE_MODEL`, so a
stand without a configured provider reports that workflow as not run, naming the cause. A suite that
sends mail declares the Mailpit address and SMTP host; that container is in this compose file under
the `development` profile.

`build_candidate.sh` is the explicit local/self-hosted evidence profile. It enables private-network
SMTP only inside the generated candidate environment so an SMTP connection installed on the
candidate can reach candidate-owned Mailpit. Calling `prepare_candidate_runtime.py` without
`--self-hosted-evidence`, ordinary production configuration, and general OSS defaults retain the
private-address and plaintext SMTP denial.
