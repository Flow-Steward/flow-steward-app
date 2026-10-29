# Historical release ledger import

The official repository starts at commit `8ccdb15b8b3f420d36c0fc882afd0146085c051b`
(27 October 2026). The prior deployed source history ends at
`6ac810b07e32ddcb148cc0c57ef0e78489f79f39` and is not an ancestor of that import.

The release-stabilization ledger is byte-identical in the prior deployed history,
the official import, and the source before this metadata repair. Its SHA-256 is
`b6f8520642d174c3d801d5a8e288d006979f7c9235be341408137a0d01b78427`.
All 56 distinct original verification and packaged-verification commit references
resolve to ancestors of the prior deployed history; none belongs to the official
repository's new ancestry. The import and legacy tip differ in 55 tracked files,
so this record does not assert whole-tree identity.

This migration carries forward the imported ledger's historical acceptance. Every
row, technical status, review status, and original evidence path is retained.
Original commit references are preserved under `legacy_historical_verified_commit`
and `legacy_historical_packaged_verified_commit`. Rows declare
`verification_basis: source_import`; active commit references name the real official
import that contains the accepted ledger.

These anchors record imported acceptance. They do not claim that the original
tests or packaged runtime checks ran at the import commit or at a later candidate.
Later QA results and any release exceptions remain separate evidence. This repair
changes no application code, ancestry validator, provenance generator, rule, or
analysis exclusion. Generated ancestor lists must still contain only real Git
ancestors of the candidate; historical SHAs must never be injected into them.
