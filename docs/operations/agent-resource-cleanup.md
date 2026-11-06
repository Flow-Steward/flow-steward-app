# Managed Agent Resource Lifecycle

The [agent workflow standard](../standards/agent-workflow-standard.md#owned-runtime-resource-cleanup)
is authoritative. This developer tool runs on the POSIX Docker host, with its existing Python
dependencies and Docker CLI. It does not run inside a product container or expose a public API.
Use one retained primary registry for all cooperating agents; do not place it in a disposable
worktree. Journals and receipts are private local JSON files, not source-controlled artifacts.

## Run a task with automatic cleanup

From a checkout containing the implementation:

```bash
python -m core.entrypoints.cli.agent_resources \
  --registry /absolute/primary-checkout/logs/resource-ownership/v1 \
  --owner TASK_ID run -- python path/to/task.py
```

`run` initializes the journal, records foreground PID/process-group leases, and creates
exclusive scratch and cache directories. The child receives `FS_TASK_SCRATCH`, `TMPDIR`,
`XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `npm_config_cache`, `FS_TASK_RESOURCE_OWNER`,
`FS_TASK_RESOURCE_TOKEN`, `FS_TASK_RESOURCE_REGISTRY` and `FS_TASK_IMAGE_PREFIX`.
It forwards SIGINT/SIGTERM only to its own process group. On exit, including a nonzero exit,
it registers remaining labelled output, releases unretained owned resources and cleans them.
A still-live child process group or audit container blocks cleanup. The original command
exit code is preserved; successful command execution with incomplete cleanup returns 2.
The receipt records `command_exit_code` and cleanup results separately.

When the command directly invokes the canonical `core/entrypoints/cli/scripts/lint.sh`,
the wrapper supplies a private Compose project and a temporary ownership-label override for
`fs-audit`, `pg-audit`, `rabbitmq-audit` and their default network. Audit build labels and the
project's audit image tag are tracked too. Host build exports and `/app/logs/tool-cache` are
redirected into the marked task cache. The original Compose files are not edited; the shared
quality-cache volume is preserved. The checkout must already have its local `.env` configured.
Sonar modes are deliberately coordinated outside this automatic quality wrapper.

Create additional directories through `mkdir NAME`; use the returned `identity` as the path.
Only newly created marked scratch directories are eligible. Copying a Git checkout into one
makes cleanup block until its unique work is separately preserved and the checkout removed
through an appropriate verified archival workflow. Do not use this command to adopt existing
project folders. Unregistered files at the workspace root are retained and reported.

## Docker ownership and registration

Resources created by a managed task need all three labels:

```bash
docker create \
  --label "org.flowsteward.task=$FS_TASK_RESOURCE_OWNER" \
  --label "org.flowsteward.resource-token=$FS_TASK_RESOURCE_TOKEN" \
  --label org.flowsteward.disposable=true \
  IMAGE COMMAND
```

The wrapper discovers only matching labelled containers, networks and volumes. The label
`disposable=true` is an explicit assertion that their contents may be discarded: never label
shared databases, SonarQube, the quality-cache volume or user-entered data disposable.
Images are auto-discovered under `"$FS_TASK_IMAGE_PREFIX:TAG"` and the exact generated
quality-project audit tag; other image tags
can be recorded only as borrowed dependencies. Owned tags must use that exclusive namespace
so another project cannot accidentally reuse a generic tag during cleanup. Prefer `docker run --rm` for one-shot checks as usual.

For explicit registration, invoke the same CLI with `track KIND IDENTITY`. Supported kinds
are `container` (full immutable ID), `network` (full immutable ID), `volume` (exact name), and
`image_tag` (exact tag). Labels, native identity and Docker daemon identity are checked both
when registering and when removing. `track ... --borrowed` records another owner's resource
as a protected dependency. Use the same registry so this claim blocks its owner's cleanup.
A volume still mounted by any container, a connected network, an active audit and an image
used by a running container are retained. No forced image removal or broad Compose down runs.

## Private build cache

Canonical managed quality commands first compute the canonical audit toolchain digest through
`lint.sh --audit-toolchain-digest` and inspect locally available images with the matching
`org.flowsteward.audit-toolchain` label. A match is pinned by its full immutable image ID,
recorded as a borrowed dependency, and used by the private Compose project. No alias is
created and no existing image is relabelled or deleted. Repeated RED/GREEN commands therefore
reuse the ready toolchain image even though their containers and scratch paths remain private.
The override removes the audit build definition with Compose's
[`!reset null`](https://docs.docker.com/reference/compose-file/merge/#reset-value).
`run` uses `--pull never`, and `up` uses `--no-build`, so this path cannot build implicitly; the canonical image guard rechecks the
digest and fails without building if the borrowed image disappears or no longer matches.

For example, repeat the same launcher with the same retained registry and task owner:

```bash
python -m core.entrypoints.cli.agent_resources \
  --registry /absolute/primary-checkout/logs/resource-ownership/v1 \
  --owner TASK_ID run -- bash core/entrypoints/cli/scripts/lint.sh --checks ruff
```

A genuinely missing matching image uses an owned image and private builder. Only that
task-created builder/cache is removed after the command. A borrowed image's active claim
is released after the foreground process and its children exit; the image itself is preserved.
The journal compares native image and daemon identities across aliases so another cooperating
owner cannot delete an image while it is borrowed.
For other build commands, run `init` and then `private-builder` with the same registry/owner arguments before `run`.
The builder uses the `docker-container` driver, a random task-specific name and owner/token
driver options. The wrapper passes its name as `BUILDX_BUILDER` to the child; default builder
selection is unchanged. After the task ends, builder removal also releases its private cache.
Daemon identity, backing container ID, cache volume creation identity and driver ownership
are recorded and checked again. An active build, another cache consumer or a changed builder identity blocks removal. Shared/default builders and
shared BuildKit cache records cannot be registered for automatic removal.

See Docker's [create](https://docs.docker.com/reference/cli/docker/buildx/create/) and
[remove](https://docs.docker.com/reference/cli/docker/buildx/rm/) references for driver behavior.

## Retention, preview and recovery

Use `retain RESOURCE_ID --consumer OWNER --reason TEXT --release-event TEXT --review-at ISO_TIME`
for a resource still needed by review or another task. Cleanup never removes it merely because
that time has passed. `release RESOURCE_ID --consumer OWNER` relinquishes a recorded consumer;
all consumers must release before the resource becomes eligible. A borrower releases its own
borrowed entry as well; borrowed entries are never deleted by the borrower.

`cleanup` previews the released-resource plan without deletion. `cleanup --apply` executes it.
Resources still `active` require `release RESOURCE_ID` after checking the task has ended.
This is also the explicit recovery path after an uncatchable termination. A live PID or process
group lease blocks removal even if the journal entries were released.

Legacy hand-written JSON journals are not accepted as executable authority. Keep them as
evidence, create a new versioned journal, and only register resources whose labels/identities
satisfy the contract. Unsupported or ambiguous resources remain for explicit owner review.
Never widen a failed cleanup to global prune or restart the shared Docker daemon.

Each apply writes `receipts/TASK_ID.json` atomically with removed IDs, retained borrowed IDs,
blockers, observed free space, and sanitized Docker inventories before and after cleanup.
Unexplained inventory changes block a successful cleanup result. Inspect remaining blockers before declaring completion.
The journal does not delete required evidence: export it to the retained checkout before the
managed command ends. Command output is inherited; do not print credentials into task logs.
