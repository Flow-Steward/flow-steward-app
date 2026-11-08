# 0.9.13 publication readiness

Prepared on 2026-11-08. This records verification scope and remaining work; it is
separate from the [product release notes](../releases/0.9.13.md).
Preparing these documents does not publish a tag, GitHub Release, or images.

## Existing evidence

| Evidence | Exact scope | Qualification |
| --- | --- | --- |
| Installer acceptance at `96900001747c114fc06edb93e69ff7c83848c8b1` | Clean Compact and Scalable installations; saved-state Scalable preflight resume with bare `./install.sh`; Compact domain/configuration changes; generated Nginx HTTPS configuration; SeaweedFS data preservation | Linux/arm64 on Docker Desktop/LinuxKit, with a private catalogue/registry, task trust/guard layers, and a raw TCP loopback adapter. This is not native Linux/amd64 or public 0.9.13 image acceptance. |
| Release upgrade at that same commit | Genuine published 0.9.12 baseline to the private 0.9.13 candidate, using literal `./flow-steward release upgrade` on both profiles with Nginx running; no timeout flag, source build, or sudo; image identities, secrets, volumes, and stored data checked | Scalable: 39.36 seconds; Compact: 50.16 seconds. The old caller's observed default was 180 seconds. Doctor reported only the expected fresh-fixture `backup_freshness` issue. |
| MCP/runtime candidate `61aef23a9988886e82e3d7284c349a581e95290a`, tree `0760f85b27efc6df59a06738b2aece60026eead0` | 78 final changed-scope checks; selected environment, lint, type, security, boundary, and localization checks; all four normal commit hooks passed | The earlier 693-test owning selection belongs to tree `38775e0b485cfd5d7dedd68adb0b91885628391c`. Installer E2E at `9690000` was not rerun after the MCP changes and does not verify `61aef23` retroactively. |

Retained local evidence is under `logs/mcp-response-limit/handoff.json` and
`logs/installer-system-verification/attempt8/reviewed-summary.json`. Those ignored
artifacts are local handoff evidence, not files distributed with the release.
Owned disposable verification resources were cleaned up.

The existing remote release branch also contains the report-chart viewer fix
`115453cba0fca73fe68ea10adf4756c9010b7311` and version preparation
`d88523ac9a35d2b2fd383e7604f5794602127a4f`. Release integration preserves both
commits. Its additional verification scope is the affected release-note/Compose
metadata tests, frontend build with the report-viewer bundle guard, and normal
commit hooks. Record the final integration SHA and check results in the PR.

## Canonical release path

- [x] Review the 0.9.13 product notes with the existing matching `VERSION=0.9.13`
  and validate the exact previous-stable comparison. Commit the reviewed notes
  in the release PR. The publication workflow revalidates and binds their digest
  to the resolved candidate before native builds.
- [ ] Freeze the final release commit. Select checks for any changes after
  `61aef23`; reuse existing results only where their input fingerprints match.
  No full, heavy, Tier 3, Sonar, or GitHub quality run is implied by this checklist.
- [ ] Complete the normal PR review and merge path to public Core main; do not
  push directly to main. Create the immutable release tag from the accepted
  source with matching version and reviewed notes.
- [ ] Prepare the final candidate's canonical image set, including the
  installer, for both supported native platforms (`linux/amd64` and
  `linux/arm64`). Verify completeness, digests, role/platform labels, revision,
  and source provenance using the official release process. Images prepared for
  `9690000` cannot establish provenance for the final commit.
- [ ] After publishing the image indexes, complete the canonical anonymous
  installer manifest and actual-pull acceptance for both architectures with
  fresh owned image stores and credential-free clients. Compare the recorded
  digests with the native build and authenticated verification evidence. Report
  each registry independently; at least one registry must be complete and public
  before the stable GitHub Release is created.

## Accepted scope and known limitations

These qualifications are not additional publication gates. An older E2E SHA
does not require rerunning the entire installer scenario solely because the
candidate SHA changed: the installer acceptance above and the verified MCP delta
remain separate evidence. Select only actual uncovered impact of subsequent
changes.

- The existing installer/upgrade E2E used an ARM64 LinuxKit fixture, not native
  customer Linux or the final public image set. Preserve that qualification.
- MCP changed-scope verification covers default/configured byte bounds, a
  response larger than 4 MiB, safe errors, actual Compose rendering for all three
  bases, and delivery to Compact child processes. A full final-image MCP replay
  is additional acceptance work, not an automatic new gate.
- Live generated-proxy acceptance covers Nginx. Apache currently has unit/render
  coverage; a live Apache replay remains follow-up work.
- Docker restart policies were checked; actual daemon/host reboot recovery was
  not established by that check. A controlled reboot replay remains follow-up.
- The read-only configurability audit's 13 delivery findings and other candidate
  limits were not fixed by this release's MCP change. They are not verified
  regressions on customer deployments or authorization to relax other policies.
  Their remediation is separate work and does not automatically block this PR.

## After publication, before promotion

- [ ] Confirm the actual public v0.9.13 tag, GitHub Release, reviewed release
  description, image provenance, and generated registry-availability footer.
- [ ] From an unauthenticated clean Docker environment, follow the published
  README installation flow and upgrade a genuine 0.9.12 installation with the
  standard command. Verify public image pulls, no source build or sudo, and
  application/CLI readiness. This public-catalogue check requires the target
  release to exist; it cannot be claimed from the private fixture acceptance.

The [official publication procedure](official-release-publication.md) remains the
source of operational publication requirements. This record makes no full-suite
or public-release verification claim.
