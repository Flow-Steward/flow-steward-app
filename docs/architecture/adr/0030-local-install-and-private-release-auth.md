# ADR 0030: Local install identity and private update authentication

## Status

Accepted — 2026-09-27. Amended 2026-10-27 (release 0.9.1): a fresh install of a
published release tag installs its official images.

## Context

Production install rediscovered its release through the anonymous GitHub API
after an operator had already cloned an exact private release. GitHub returned
404 and blocked installation before public OCI images could be resolved. Clone
also carried an exact authenticated identity but performed remote discovery.

## Decision

Fresh production install builds the local checkout through the existing Docker
build paths for Compact and Scalable. It records source mode and the full Git
commit SHA; a release tag and remote release discovery are not prerequisites.
This corrects the original tagged-checkout-only decision after a real VPS
installation failed on `production_checkout_tag_missing` (user clarification,
2026-09-27). Existing official identities and activation journals still use the
complete-set OCI resolver and never silently fall back to local builds.

Amendment (2026-10-27, once the repository and both registries became public):
a fresh install with no installer state whose checkout is clean and exactly at
its `v<VERSION>` tag installs that release officially. The installer asks the
local checkout through a non-raising probe, so an ordinary development checkout
(no tag, the tag elsewhere, tracked changes, no usable Git) keeps the source
build above. At the tag it reads the stable GitHub Release for that version from
`FS_RELEASE_REPOSITORY`, requires the published tag to name the checked-out
commit, resolves the complete digest-pinned role set from the first configured
registry that has all of it, and stages it as the pending release, so the
existing official install path pulls by digest and builds no product image. A
release that is not published as stable, an incomplete image set, or an
unreachable GitHub or registry stops the install with
`release_images_unavailable`; it never falls back to a source build. A resumed
source installation is not re-decided from the checkout.

The installed origin is one of two explicit states. A source installed origin
is accepted only from a completed installer/configuration record whose complete
runtime image set proves the same full source commit. An official installed
origin is the existing verified, digest-complete release identity. The
official-only `installed-release.json` remains the sole durable official
identity and is never synthesized from `VERSION`, the checkout, or source image
labels.

For source-built Compact, product CLI and MCP remain embedded, but host management
uses a separate immutable image built from the existing `cli` target. Docker,
Git and PostgreSQL client tools belong to that management image; they are not
added to Compact runtime. Preparation records its full source revision and an
owner-only `compact-cli-image.json` build-ownership tuple so lost or stale
installer-built images can be rebuilt without replacing operator-selected pins.
Resume requires management provenance at the active source revision. Its
candidate preflight executes the product-owned database and worker-identity
checks inside the installed Compact runtime, not the management source slice.
A separate MCP image is optional. Any explicitly selected separate images must
carry the same full revision and their provenance is persisted during preparation.
This does not reduce the complete published role set required for an official
release identity.
The shared environment generator does not invent a tagged MCP selection for
production Compact when none was supplied, including later configuration
regeneration. Existing or explicitly supplied image selections are preserved;
this does not silently repair an older persisted, invalid image selection.

For a source origin, semantic ordering is unknown: a matching, ancestor,
descendant, or unrelated published commit is still presented as an explicit
transition, never inferred to be an upgrade, downgrade, or current release. The
source-to-official operation must be reviewed and explicitly confirmed. Its
source origin, active configuration fingerprint, repository, profile/platform,
and exact official target are bound during review and revalidated under the
installer lifecycle lock before pending state, tag checkout, or runtime
mutation. The durable handoff keeps that exact binding across interruption and
resume.

Scalable release upgrade runs in a disposable management container with the
installation's normal CLI mounts and ownership. Replacing the retained CLI
service must not terminate the process that completes the release handoff.
Every Docker management service declares Docker installer execution explicitly
and maps `host.docker.internal` to the host gateway. Child release handoffs inherit
that context; a generic handoff does not rewrite explicitly selected host execution.
Compact forwards the internal configuration-only Doctor section to its one-shot
management CLI, preventing a full installation diagnosis from nesting inside the
outer host diagnosis.
An explicit official target whose identity differs from the installed identity
uses the existing ordered update path even when source and target share a commit.
Image preparation therefore precedes worker bootstrap and runtime use; Git revision
equality alone does not authorize the already-installed shortcut. An unchanged
installed official identity remains idempotent.
An explicitly confirmed installer resume can reuse a durable source-transition
binding only with the same pending target, handoff, repository, and configuration
fingerprint. The private installer rechecks fully enabled maintenance before
changing either source or official runtime. Scalable reads that marker through
the installation state root; Compact keeps its marker in the data-volume state.

The installer keeps the published version-1 `release-handoff.json` wire shape
stable because the checked-out target release parses that file before it can
install itself. Source-transition authority is therefore stored as a separate
owner-only operation artifact in the existing `InstallerStateStore`; it is not
a second installation or provenance store. Under the operation lock, a new
transition revalidates its source, verifies a missing exact tag in an isolated
temporary bare repository, and checks tracked/untracked checkout safety before
writing the binding, the compatible prepared journal, and the pending target in
that order. Only then may the verified tag enter the installation checkout and
change `HEAD`. Each durable prefix is resumable without selecting a new latest
release.

A completed handoff whose installed identity already equals its target and has
no pending marker is terminal. Common release activation, including Clone,
verifies the exact `HEAD` and returns idempotently without invoking a target
installer that requires pending state. Crash reconciliation publishes that
terminal journal and clears the source-transition operation binding first.

Only release check, release upgrade, and a fresh install of a release tag
perform GitHub discovery. They may read an optional token from `.flow-steward/secret-input/github-release-token`. The
directory is owner-only mode `0700`; the bounded regular non-symlink file is
mode `0600`. Discovery sends the token only to `https://api.github.com`. When a
release transition needs a missing exact tag, Git fetch may also authenticate to
the configured repository at `https://github.com/<repository>.git` through a
temporary owner-only askpass helper. That helper reads the same private token
file and answers only the `github.com` password prompt; credential helpers,
global/system Git configuration and HTTP redirects are disabled for that fetch,
and the temporary helper is removed afterwards. Only the credential-file path
enters the helper's environment. The token value is never persisted in arguments,
environment, Compose, journals, release identity, or Clone data. GitHub release
authentication remains separate from registry authentication.

Clone has an official-only Clone prerequisite on both source and target. A
source installation must complete the explicit release transition first. Clone
then verifies the digest-pinned identity authenticated by its manifest through
the same registry resolver and performs no GitHub discovery.

Host-directory Clone publication preserves the exact mounted root rather than
renaming a container bind mount. A disposable digest-pinned management helper
receives only the reviewed target directory and private staging scratch; it
publishes authenticated children resumably without mounting the parent or
unrelated roots. Host ownership follows the retained target UID/GID, independently
of source or Compact-volume ownership. The existing Compact-volume publication
and mixed-placement durable-state boundaries remain unchanged.

## Consequences

- Private source access is needed to clone; a fresh install of a release tag reads
  the published release, other fresh installs do not.
- Updater authentication is deployment-local and removable when source is public.
- Local Git, GitHub metadata, OCI identity, and installed state retain separate
  trust responsibilities.
- Official update and Clone share the resolver and activation engine. Checkout
  installations use the existing source-build provenance and resume paths;
  they do not fabricate a published release identity from an untagged commit.
