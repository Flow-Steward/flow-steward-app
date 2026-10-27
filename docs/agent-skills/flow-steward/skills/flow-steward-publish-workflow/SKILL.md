---
name: flow-steward-publish-workflow
description: Use when a Flow Steward user wants to share a workflow as a template in the public workflow catalog Flow-Steward/workflows, or publish a new version of one; covers naming, export with child workflows, privacy review, category folder and the catalog pull request.
---

# Publish a Flow Steward Workflow Template

The public catalog is <https://github.com/Flow-Steward/workflows>. Flow Steward
reads its `index.json` and lists the templates under **Workflows → Catalog**.
Each template is one exported YAML file in that repository. The catalog README
(`README.md`, section "Share a workflow") is the source of truth: read the
current copy before you start and follow it where it differs from this skill.

Publishing makes the workflow public. Get the user's explicit go-ahead before
opening the pull request.

## 1. Prepare the workflow in Flow Steward

- **Name** (`display_name`): a readable title, not an identifier —
  "Import supplier stock from email attachments", not `stock_import`. At most
  120 characters.
- **Description**: at least 40 characters. Its first sentence becomes the card
  summary, so say what the workflow does.
- **Version**: semantic, such as `1.0.0` (the default for a new workflow). A
  version already published in the catalog for the same `workflow_id` is
  refused; raise it for every new submission. The workflow screens show no
  version field; an administrator can change it through the workflow metadata
  API (read the workflow, then a full `PUT /api/workflows/<workflow_id>` with
  the same metadata and the new `version`; see the API reference). Confirm the
  new version in the exported file.
- It must be a current, phase-free workflow. A phase-based
  `platform.workflow_bundle.v1` export is refused.
- Validate it and run it at least once (a controlled Test Run), so the template
  is known to work.

## 2. Export

In the UI: **Workflows**, the workflow's **⋯** menu, then **Export YAML**. If
it calls other workflows, choose **Export YAML with child workflows**, so they
travel in the same file and the template installs complete. Through MCP:
`workflow_export(project_name, workflow_id, include_children=true)`.

One file is one template: the main workflow and the child workflows it calls,
nothing else. Do not hand-edit the exported file.

## 3. Privacy review

Exports leave configured secret values out, and the catalog check refuses a
value under keys such as `password`, `secret`, `api_key`, `access_token` or
`private_key`. That is not enough on its own. Read the whole file with the
user and look for:

- literal values in step settings and mappings: email addresses, customer or
  shop names, internal URLs and host names, file paths, account ids;
- prompts and notes that quote real data;
- sample data embedded in inputs.

If something private is there, change it in Flow Steward (use a placeholder or
an input) and export again, rather than editing the file.

## 4. Add the file to the catalog

1. Fork `Flow-Steward/workflows` and create a branch.
2. Pick one category folder from the repository's `categories.json` (for
   example `ai`, `crm`, `ecommerce`, `email`, `marketing`, `operations`,
   `other`). The category is the author's choice; reviewers only check that it
   is in the list.
3. Add the export as `workflows/<category>/<workflow_id>.yaml`. For a later
   version, add a **new** file next to the old one, for example
   `workflows/email/mailbox_search_1_1_0.yaml`. Never change or delete an
   already published file: every catalog entry pins its file's exact bytes.
   (The one exception: the pull request that adds the v2 export of a
   phase-based v1 file may remove that v1 file.)
4. Do not edit `index.json` or `categories.json`. The index is rebuilt by a
   GitHub Action after the merge; the category list is owned by the extension
   catalog.

Optionally run the check locally from the catalog clone (it uses a Python
virtual environment of the catalog repository, not Flow Steward):

```bash
pip install -r requirements.txt
python scripts/build_index.py --check --base origin/main
```

## 5. Open the pull request

Title: "Add <workflow title> <version>". In the description say what the
workflow does, which extensions and connections it needs, and that it was
exported with child workflows when it has any. With `gh`:

```bash
gh pr create --repo Flow-Steward/workflows --title "Add <title> <version>" --body-file pr.md
```

The **Validate submission** check refuses, among others: a file that is not
`platform.workflow_bundle.v2`, a called workflow missing from the file, an
extra workflow the main one does not call, a folder that is not a category, an
identifier-style name, a short description, a non-semantic version, a secret
value, a file larger than 2 MB, and a change to a published file. Fix what it
reports in Flow Steward, export again and push.

After the merge, a GitHub Action rebuilds `index.json`. Each Flow Steward
server caches the index for up to one hour, so the template appears there
within about an hour.

## Report

Return the workflow id, version, category, file path, the pull request URL and
the check result. Say what the privacy review found and changed.
