# Product release notes

The canonical rules are in
[Product Release Notes](../standards/agent-workflow-standard.md#product-release-notes).

For every new Core version:

1. Copy [TEMPLATE.md](TEMPLATE.md) to `<version>.md` in this directory and update
   `VERSION` in the same release candidate.
2. Review the actual changes since the previous **published stable release tag**.
   Write concrete features, improvements and bug fixes grouped by product area.
   Describe the resulting behavior rather than pasting commit subjects or a list
   of internal branches. Omit empty categories; a fix-only release needs no
   invented features.
3. Replace every placeholder, include the full comparison link and document any
   compatibility, breaking-change or upgrade requirements. Only describe shipped
   Core changes; a separate extension publication is a separate release.
4. Review the content before committing and creating the tag. The workflow reads
   the file from the tagged commit, determines the previous stable release from
   GitHub metadata, and rejects missing/invalid notes before building images.
5. Publication rechecks the file digest. It creates the release from these notes,
   preserves valid subsequent human edits on retries, and appends current registry
   availability at the bottom. It never invents a full quality-gate pass.

An optional local content check, before tagging, is:

```bash
python core/entrypoints/cli/scripts/release-automation.py notes \
  --tag v0.9.13 --previous-tag v0.9.12
```

The local content check does not replace the workflow's tag, source, registry or
verification requirements. Automated publication uses actual published release
metadata instead of trusting the author's previous-tag choice.

[0.9.12.md](0.9.12.md) is an example of product-facing notes. Registry availability
is supplied by publication, not copied into the next version's source notes.
Quality exceptions and unverified acceptance must be stated honestly, separately
from the product changelog.

For historical immutable tags that predate this process, rerun their original
workflow run when repairing publication. A new workflow cannot add a missing
notes file to an already existing immutable tag.
