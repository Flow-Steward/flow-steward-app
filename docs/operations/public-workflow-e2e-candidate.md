# Public workflow E2E candidate deployment

Owner: Release Engineering

GitHub Actions are disabled. Candidate builds, deployment, and verification run
locally in Docker against an explicitly selected commit and image digest.

For a release candidate, the operator must:

1. Build and validate the candidate through the local procedure in
   [official-release-publication.md](official-release-publication.md).
2. Verify the candidate archive checksums and deploy those images without
   rebuilding them.
3. Set `FS_BUILD_SHA` to the candidate commit and `FS_IMAGE_DIGEST` to the
   deployed application image digest.
4. Provision the public-workflow project, workflow, approval step, machine token,
   PostgreSQL, RabbitMQ, worker, and persistent artifact storage.
5. Set `PUBLIC_WORKFLOW_E2E_BASE_URL` and the required test credentials locally.
6. Check `/health` for the expected SHA and image digest, then run the relevant
   integration/E2E checks in Docker.
7. Retain the local verification evidence for that candidate and clean up the
   temporary containers, volumes, networks, and images after verification.

An unavailable deployment, stale SHA, different digest, or failed check blocks
publication. Publish the same validated candidate archives. There is no GitHub
workflow, GitHub artifact download, or remote status prerequisite.
