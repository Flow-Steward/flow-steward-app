---
name: flow-steward-publish-extension
description: Use when a developer publishes a Flow Steward extension or a new version, registers a first-party release, or verifies delivery to the public Flow-Steward/extensions catalog.
---

# Publish a Flow Steward Extension

The public catalog is <https://github.com/Flow-Steward/extensions>. Flow
Steward reads its `index.json` and lists the entries under
**Extensions → Catalog**. The catalog holds only a **link** to the author's own
repository; the extension's code and releases live there. The catalog README
(`README.md`, section "Share an extension") is the source of truth: read the
current copy before you start and follow it where it differs from this skill.
Also locate the target Flow Steward application checkout and read its canonical
`docs/standards/extension-publication-standard.md` for official artwork,
bundled provenance, and publication review. Catalog acceptance alone does not
establish that a logo is official or visually faithful.

Build the extension first with the `flow-steward-extension-authoring` skill.
Publishing does not install anything; never install, enable or publish without
the user's explicit request.

This work needs a terminal with Git and Docker, a Flow Steward application
checkout for the quality gate, and GitHub access (your client's GitHub tools or
an authenticated `gh`) to push tags and run/watch catalog registration CI for
first-party repositories, or open a catalog pull request for external authors.
It does not need a Flow Steward MCP connection. When access is missing, name
the blocked step and do not report it as done.

## 1. One public repository per extension

- `extension.yaml` sits at the **repository root**.
- Name the repository `ext-<kind>-<implementation>` (for example
  `ext-llm-openrouter`) and give it the GitHub topic `flow-steward-extension`.
- To move an extension out of a larger checkout, the catalog provides
  `scripts/new_extension_repo.py <extension folder> ../ext-<kind>-<name> --repository https://github.com/<owner>/ext-<kind>-<name>`.
  It copies the tracked files and adds the CI and release workflows from the
  catalog's `templates/single-extension/`.
- The id namespace `flowsteward.` is reserved for github.com/Flow-Steward
  (`verified_publishers.json`). Use your own reverse-domain prefix, such as
  `com.acme.crm-sync`.

## 2. Manifest fields the catalog checks

In `extension.yaml`:

- `display_name`: a readable title.
- `description`: at least 40 characters.
- `listing.category`: one id from the catalog's `categories.json` (for example
  `ai`, `crm`, `data`, `ecommerce`, `email`, `messaging`, `other`).
- Optional: `listing.summary` (at most 160 characters; otherwise the first
  sentence of the description), `listing.tags`, `listing.setup_summary`.
- `runtime.contract_version: extension_host_v2`, built on
  `flowsteward-extension-sdk` 0.4.0 or later. Releases without it are not listed.
- `egress` declaring the outbound access (`egress: {}` for none); otherwise
  installation warns `egress_undeclared`.
- `version`: semantic (`1.2.0`). If the bundle has `extension-descriptor.yaml`,
  its `extension_id` and `release_version` must match the manifest.

Next to `extension.yaml`, `icon.png`: PNG only, square, 32–512 px (128×128
recommended), at most 100 KB. A release without a valid icon is refused by the
catalog and by Flow Steward's installer.
Complete the canonical publication standard's branding review before the first
release, then verify every target archive and the generated Marketplace card.

Never commit secrets: the check refuses `.env` and key files and any value
under keys such as `password` or `api_key`.

## 3. Validate and test

From a Flow Steward application checkout installed in the Development
environment, run the quality gate against the committed extension checkout:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root /abs/path/to/extension-checkout
```

It stages the checkout under its canonical folder name, validates the manifest,
runs ruff, mypy and bandit, and runs the extension's tests offline. For the
catalog's listing checks, also run `extensions validate` with `--marketplace`
on a copy placed as `extensions/<canonical folder>/` in the application
checkout (do not commit that copy):

```bash
./flow-steward extensions validate <extension_id> --root extensions --health --marketplace
```

`--marketplace` adds the identity, listing, vendor and scope checks. Stop on
any nonzero exit and fix the cause. The CI workflow from the catalog template
(step 5) also runs the catalog's own archive check
(`.github/scripts/catalog_check.py`) on every push.

## 4. Dependencies and architectures

Flow Steward runs on Linux AMD64 and ARM64, and installs offline from the
bundle's `wheels/`.

- **No third-party Python packages**: one archive serves both.
- **Third-party Python packages**: lock the full dependency closure with
  `extensions lock-deps`, then `extensions bundle --with-wheels`. Its default
  platforms are `manylinux2014_x86_64,manylinux2014_aarch64`, so pure-Python
  wheels and compiled wheels for both targets land in `wheels/` of one
  archive.
- **Or one archive per architecture** for compiled wheels (numpy,
  cryptography, onnxruntime...): `<extension_id>-<version>-linux-amd64.zip` and
  `<extension_id>-<version>-linux-arm64.zip`, each with only the wheels for its
  target and a `.fs-package.yaml` naming that target. The catalog's
  `templates/single-extension` packaging script does this when `wheels/` holds
  architecture-specific wheels, and the catalog README describes this layout.
- **Native executable**: one archive per target, each built with
  `flow-steward extensions bundle --runtime-target linux-amd64` (and
  `linux-arm64`), carrying only its own `bin/<target>/`.

A release attaches either one portable archive or per-target archives, never
both. Per-target releases must include `linux-amd64`.

The runtime is CPython 3.12 on glibc 2.36. For packages that publish only
`manylinux_2_28` wheels, pass
`--platforms manylinux_2_28_x86_64,manylinux_2_28_aarch64` to both `lock-deps`
and `bundle`. `extensions validate` does not check both architectures: list
`wheels/` and confirm, for every requirement, an `_x86_64` and an `_aarch64`
wheel or one `any` wheel. If a wheel is missing for one target, stop and report
it; never ship an archive that downloads packages at runtime or silently drops
a target.

## 5. Release

Archive rules the catalog enforces:

- tag `v<version>`, the same version as `extension.yaml`;
- asset `<extension_id>-<version>.zip` (or the per-target names above);
- every file under one top folder named after the id with `.` and `-` replaced
  by `_` (`acme.crm-sync` → `acme_crm_sync/`);
- no symbolic links, at most 50 MB zipped and 150 MB unpacked.

The easiest path is the catalog's template workflows: copy
`templates/single-extension/.github/` (CI on every push: tests, packaging and
the catalog's own archive check; release on every tag), or the simpler
`templates/release-extension.yml`. Then, with the user's go-ahead:

```bash
git tag v1.2.0
git push origin v1.2.0
```

Check that the GitHub Release exists with the expected asset(s).

## 6. Register the first listing

### Public Flow-Steward/ext-* repositories: catalog CI

After the stable GitHub Release and its expected archives exist, run the
catalog-owned workflow on its main branch. Substitute the actual repository
and published tag in this example:

```bash
gh workflow run register-extension.yml \
  --repo Flow-Steward/extensions --ref main \
  -f repository=Flow-Steward/ext-finance-b1 \
  -f tag=v0.1.0
```

The workflow reads the manifest at the resolved tag commit and creates
`extensions/<extension_id>.yaml` itself. The id comes from the manifest,
not the repository slug. For a variant manifest, add
`-f manifest_path=variants/b1/extension.yaml`. The repository preparation
script prints the registration command for maintained repositories.

Capture the returned run URL/ID, then wait for that run with
`gh run watch RUN_ID --repo Flow-Steward/extensions --exit-status`
(replace RUN_ID). If the client returns no run URL, identify the run whose
repository/tag appear in its registration receipt; do not assume the latest
run belongs to this release.

Require a successful workflow and its **Verify the exact public catalog
commit** step. Dispatch accepted, queued, cancelled or failed is not delivery.
The workflow validates archives, preserves their hashes through publication,
publishes the index and rechecks the selected release and public catalog.
A successful identical rerun reports `changed: false`; that is valid evidence.

This command uses the catalog's existing job token, not a new token in the
extension repository. It requires permission to run catalog workflows. If that
access is absent, report registration as blocked and request an authorized
maintainer to run it; do not request pasted tokens or switch to hand-written
first-party registrations. Conflicting IDs, moved tags or changed published
bytes must be diagnosed, never overwritten.

### External author repositories: catalog pull request

The first-party workflow accepts only public `Flow-Steward/ext-*`
repositories. For another owner, fork `Flow-Steward/extensions` and add
exactly `extensions/<extension_id>.yaml`, using the id from the manifest:

```yaml
repository: https://github.com/<owner>/<repo>
```

Open the PR from the repository owner's account or a public member of its
owning organization. Require **Validate submission** to pass and the PR to be
merged, then verify catalog delivery below. `index.json` and `releases/`
are generated by the catalog workflows; do not edit them.

## 7. New versions

Bump `version` in `extension.yaml` (and `release_version` in the descriptor),
commit, then push a new `v<version>` tag. **No catalog pull request is needed**:
the catalog checks listed repositories every five minutes and publishes each new
release that passes. A failing release is reported in the catalog's Actions log
and the previous version stays listed. For first-party repositories, repeat the
registration CI command with the new tag to verify delivery explicitly. For
external authors, inspect catalog publication and the public index after it
runs. A green extension Release job alone does not finish publication.

Never move or replace a published tag or asset. The catalog records each
release's URL, size and SHA-256 once in `releases/<extension_id>/<version>.json`;
if the asset's bytes change later, that release is withdrawn. To fix a release,
publish a new version.

## 8. Verify catalog delivery

Read the public catalog after publication through the client's GitHub tools or:

```bash
gh api -H 'Accept: application/vnd.github.raw+json' \
  repos/Flow-Steward/extensions/contents/index.json
```

Require exactly one item with the manifest's `extension_id`, with
`publication_status: published`, a version equal to or newer than the
requested version (compare SemVer, not strings), and `repository_url`
pointing to the expected source repository and observed tag. Check the archive
URLs, byte sizes and SHA-256 values, including every `release_targets`
entry, against the validated release records. When a newer version is selected,
verify that version's archives, not only the older requested tag; the first-party
workflow performs this check before and after publication.

Missing, stale, duplicate, withdrawn or mismatched entries are incomplete
delivery. Inspect the registration run's failed logs and catalog publisher logs;
report published source release and pending/failed catalog delivery separately.
Do not report completion on the expectation that the next scheduled run will
fix it. Never repair this by editing generated records or replacing release bytes.

## Report

Return the source repository, manifest id, requested tag, archive names/sizes/
SHA-256 values and validation/gate results with exit codes. Catalog evidence
must include:

- First-party registration run URL and successful conclusion, or the merged
  external-author catalog PR URL.
- Observed catalog revision, item id, version, published status, repository URL
  and matching archive identity, including per-target assets.
- Anything unverified, such as installation on ARM64 or live-provider behavior.

An idempotent `changed: false` run may complete the task when its delivery
checks succeed. A release without verified catalog delivery may not.
