# Publishing the agent skill packages

How the Operator and Developer packages are released with Flow Steward and
submitted to the AI-client catalogs. Statuses and per-client limits are in
[COMPATIBILITY.md](../COMPATIBILITY.md).

| File | Use |
| --- | --- |
| [data-handling.md](data-handling.md) | What the packages read, send and keep; answers for the catalogs' data questions |
| [anthropic.md](anthropic.md) | Anthropic directory: two plugin bundle submissions from this repository |
| [openai.md](openai.md) | OpenAI plugin directory: two skills-only zip uploads |
| [cursor.md](cursor.md) | Cursor Marketplace: a publication repository and one submission |

Listing names, descriptions, categories and keywords come from
`docs/agent-skills/packages.json` and are rendered into each package's
manifests by `infra/tooling/agent-skills/sync_packages.py`. Change them there,
never in a manifest or in a catalog form only. Both packages use the product
icon (the favicon of the web UI on a square canvas), generated into each
package as its icon file; no other logo is used.

## Owner inputs still missing

Each catalog submission is blocked until its inputs exist. Local installation
and the GitHub Release archives are not blocked.

| Input | Needed by | Placeholder |
| --- | --- | --- |
| Privacy policy URL (HTTPS) | Anthropic (`privacyPolicyUrl`), OpenAI (`privacyPolicyURL`), Cursor listing | `TODO(owner): privacy policy URL` |
| Support URL or contact | Anthropic (`supportUrl`), OpenAI (`supportURL`), Cursor listing | `TODO(owner): support URL` (proposed: `https://github.com/Flow-Steward/flow-steward-app/issues`) |
| Terms of service URL | OpenAI (`termsOfServiceURL`), Anthropic (`termsOfServiceUrl`, optional) | `TODO(owner): terms of service URL` |
| Contact email for review correspondence | Anthropic compliance step, OpenAI review mail | `TODO(owner): publisher contact email` |
| Anthropic publisher: claude.ai organization on a plan that can submit, a role that can submit, GitHub account with push access to `Flow-Steward/flow-steward-app` connected in that organization | Anthropic | `TODO(owner): Anthropic publisher account` |
| OpenAI publisher: organization owner or a member with Apps Management Write, completed business verification | OpenAI | `TODO(owner): OpenAI publisher account` |
| Cursor: a public publication repository under `Flow-Steward` and an account to submit it | Cursor | `TODO(owner): Cursor publication repository` |

When a URL is known, add it to `packages.json` only if the generator gains a
field for it; until then enter it in the catalog form. Do not put a placeholder
URL into a shipped manifest.

## Release the archives with a Flow Steward release

The archives are built from a committed, tagged Core revision. Nothing is
uploaded automatically; there is no release service and no CI job.

1. Raise `version` in `docs/agent-skills/packages.json` for every package whose
   files changed since the last release, run
   `python3 infra/tooling/agent-skills/sync_packages.py`, and commit. Clients
   that cache by version do not update a package whose version did not change.
2. Tag the Core release as usual (for example `v0.9.7`).
3. From a clean checkout of that tag, run:

   ```bash
   python3 infra/tooling/agent-skills/build_release.py --ref v0.9.7
   ```

   It refuses to build when a generated package file differs from its source at
   that revision, and writes into `dist/agent-skills/<commit>/` (ignored by
   Git):

   - `flow-steward-operator-<version>.zip` and `.tar.gz`
   - `flow-steward-developer-<version>.zip` and `.tar.gz`
   - `flow-steward-agent-skills-<Core version>.zip`, a marketplace folder with
     both packages
   - `SHA256SUMS`
   - `release-manifest.json`: Core repository, commit, tag and version, each
     package's version, file list and archive digests

   Building twice from the same commit gives the same bytes; compare
   `SHA256SUMS` from two builds when in doubt.
4. Open the GitHub Release of that tag in `Flow-Steward/flow-steward-app` and
   attach all seven files, for example
   `gh release upload v0.9.7 dist/agent-skills/<commit>/* --repo Flow-Steward/flow-steward-app`.
5. Name the package versions and the Core tag in the release notes, and link
   [COMPATIBILITY.md](../COMPATIBILITY.md).

A catalog submission uses the same files: the OpenAI directory takes the
package zip, the Anthropic directory reads the tagged plugin folder, and the
Cursor publication repository is filled from the marketplace archive.

## Order of work for the publisher account owner

1. Provide the inputs in the table above.
2. Release the archives (above) for the Core tag the catalogs should show.
3. Anthropic: follow [anthropic.md](anthropic.md), one submission per package.
4. OpenAI: follow [openai.md](openai.md), one upload per package.
5. Cursor: follow [cursor.md](cursor.md).
6. Record each step in the status columns of [COMPATIBILITY.md](../COMPATIBILITY.md):
   **Submitted** when a review starts, **Published** when the listing is live.
