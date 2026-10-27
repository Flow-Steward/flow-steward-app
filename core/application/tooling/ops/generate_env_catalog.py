from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from core.infrastructure.config.env_audit import write_env_audit_artifact

EXAMPLE_FILES = (
    ".env.example",
    ".env.example.prod",
    ".env.example.ops",
    ".env.example.memory",
    ".env.example.test",
    ".env.example.quality",
)

SCOPE_LABELS = {
    "public_operator": "Public / Operator",
    "test_config": "Test Config",
    "internal_bridge": "Internal Bridge",
    "internal_runtime": "Internal Runtime",
    "script_internal": "Script Internal",
    "test_only": "Test Only",
    "tooling_internal": "Tooling Internal",
    "uncategorized": "Uncategorized",
}

USAGE_PREFIX_LABELS = (
    ("infra/docker/", "Docker Compose"),
    ("core/entrypoints/api/", "API"),
    ("core/entrypoints/admin/", "Admin"),
    ("core/entrypoints/web/", "Web entrypoints"),
    ("core/entrypoints/ws/", "WebSocket entrypoints"),
    ("core/entrypoints/mcp/", "MCP"),
    ("core/entrypoints/cli/scripts/", "CLI scripts"),
    ("core/entrypoints/cli/", "CLI"),
    ("core/infrastructure/runtime/worker/", "Worker runtime"),
    ("core/infrastructure/runtime/control/", "Control plane"),
    ("core/infrastructure/config/", "Config"),
    ("core/infrastructure/db/", "Database"),
    ("core/infrastructure/security/", "Security"),
    ("core/infrastructure/", "Infrastructure"),
    ("core/application/tooling/", "Tooling"),
    ("core/application/jobs/", "Jobs"),
    ("core/application/workflows/", "Workflows"),
    ("core/application/reports/", "Reports"),
    ("core/application/", "Application"),
    ("assets/frontend/", "Frontend"),
    ("extensions/", "Extensions"),
    ("tests/", "Tests"),
)


def _read_env_names(path: Path) -> set[str]:
    names: set[str] = set()
    if not path.exists():
        return names
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.add(line.split("=", 1)[0].strip())
    return names


def _read_env_contexts(path: Path) -> dict[str, tuple[str, ...]]:
    if not path.exists():
        return {}
    contexts: dict[str, set[str]] = defaultdict(set)
    section = ""
    pending_comments: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            pending_comments = []
            continue
        if stripped.startswith("#"):
            comment = stripped.lstrip("#").strip()
            normalized = comment.strip("- ").strip()
            if not normalized:
                continue
            if stripped.startswith("# ---") and stripped.endswith("---"):
                section = normalized
                pending_comments = []
                continue
            lowered = normalized.lower()
            if lowered.startswith(("quick start", "optional overlays", "append to")):
                continue
            if normalized.startswith(("cp ", "cat ", "./")):
                continue
            pending_comments.append(normalized)
            continue
        if "=" not in stripped:
            pending_comments = []
            continue
        key = stripped.split("=", 1)[0].strip()
        if not key:
            pending_comments = []
            continue
        row_context: list[str] = []
        if section:
            row_context.append(section)
        if pending_comments:
            row_context.extend(pending_comments[-2:])
        if row_context:
            contexts[key].add("; ".join(dict.fromkeys(row_context)))
        pending_comments = []
    return {key: tuple(sorted(values)) for key, values in contexts.items()}


def _example_contexts(repo_root: Path) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = defaultdict(set)
    for filename in EXAMPLE_FILES:
        for name, contexts in _read_env_contexts(repo_root / filename).items():
            for context in contexts:
                result[name].add(f"{filename}: {context}")
    return {name: tuple(sorted(values)) for name, values in result.items()}


def _read_class(locations: list[str]) -> str:
    if not locations:
        return "no_reads"
    buckets = {
        "tests"
        if loc.startswith("tests/")
        else "docs"
        if loc.startswith("docs/")
        else "runtime"
        if loc.startswith(("core/", "infra/", "extensions/", "assets/", "db/"))
        else "other"
        for loc in locations
    }
    if buckets == {"tests"}:
        return "tests_only"
    if buckets <= {"tests", "docs"}:
        return "tests_docs_only"
    if "runtime" in buckets:
        return "runtime_present"
    return "other"


def _scope(row: dict, manifest_map: dict[str, dict]) -> str:
    name = row["variable_name"]
    locations = row.get("code_read_locations") or []
    documented = bool(row.get("documented_sources"))
    manifest_row = manifest_map.get(name)
    if manifest_row is not None:
        groups = set(manifest_row.get("groups", []))
        if "test_tools" in groups:
            return "test_config"
        if "quality_tools" in groups or "audit_tools" in groups:
            return "tooling_internal"
        if manifest_row.get("installer_managed") or row.get("documented_sources"):
            return "public_operator"
        if name.startswith(("FS_PROJECT_", "FS_DEPLOYMENT_DOCTOR_", "FS_MCP_ALLOWLIST_")):
            return "internal_bridge"
        return "internal_runtime"
    if name.startswith(("FS_PROJECT_", "FS_DEPLOYMENT_DOCTOR_", "FS_MCP_ALLOWLIST_")):
        return "internal_bridge"
    if documented:
        if name.startswith(("E2E_", "TEST_", "PYTEST_", "NIGHTFIRE_")) or name in {
            "APP_BASE_URL",
            "TEST_RUNNER_BASE_URL",
        }:
            return "test_only"
        if name.startswith(
            (
                "SONAR_",
                "FS_COV_",
                "FS_SEMGREP_",
                "FS_ZAP_",
                "FS_VULTURE_",
                "FS_PYLINT_",
                "FS_XENON_",
                "FS_QUALITY_",
            )
        ):
            return "tooling_internal"
        return "public_operator"
    if name.startswith(("E2E_", "TEST_", "PYTEST_")) or _read_class(locations) == "tests_only":
        return "test_only"
    if name.startswith(("SONAR_", "ACTIVE_DRIVER", "ACTION", "AGENTS", "AGENT_COUNT")):
        return "tooling_internal"
    if name.startswith(("FS_", "FLOW_STEWARD_")):
        return "internal_runtime"
    if not row.get("documented_sources") and any(
        loc.startswith("core/entrypoints/cli/scripts/") for loc in locations
    ):
        return "script_internal"
    return "uncategorized"


def _usage_label(location: str) -> str:
    for prefix, label in USAGE_PREFIX_LABELS:
        if location.startswith(prefix):
            return label
    first = location.split("/", 1)[0]
    return first or "Other"


def _usage_areas(locations: list[str]) -> str:
    if not locations:
        return "No direct literal reads found"
    counter = Counter(_usage_label(location) for location in locations)
    return ", ".join(f"{label} ({count})" for label, count in sorted(counter.items()))


def _locations_preview(locations: list[str], *, limit: int = 6) -> str:
    if not locations:
        return ""
    shown = locations[:limit]
    suffix = []
    if len(locations) > limit:
        suffix.append(f"+{len(locations) - limit} more in JSON audit")
    return "<br>".join([*(f"`{location}`" for location in shown), *suffix])


def _shorten(value: object, *, limit: int = 120) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _md_cell(value: object) -> str:
    text = str(value or "-")
    return text.replace("|", "\\|").replace("\n", "<br>")


def _code_or_dash(value: object, *, limit: int = 120) -> str:
    text = _shorten(value, limit=limit)
    if not text:
        return "-"
    return f"`{_md_cell(text)}`"


def _yes_no(value: object) -> str:
    return "yes" if bool(value) else "no"


def _description(
    *,
    row: dict,
    manifest: dict,
    group_metadata: dict[str, dict],
    contexts: tuple[str, ...],
) -> str:
    notes = str(manifest.get("notes") or row.get("notes") or "").strip()
    if notes:
        return notes
    if contexts:
        labels: list[str] = []
        for context in contexts:
            _, _, label = context.partition(": ")
            labels.append(label or context)
        return "; ".join(dict.fromkeys(labels[:3]))
    groups = manifest.get("groups", row.get("conditional_groups", [])) or []
    descriptions = [
        str(group_metadata.get(str(group), {}).get("description") or "").strip() for group in groups
    ]
    descriptions = [description for description in descriptions if description]
    if descriptions:
        return "; ".join(dict.fromkeys(descriptions))
    if manifest:
        owner = str(manifest.get("owner") or row.get("owning_subsystem") or "runtime")
        return f"Canonical manifest variable owned by `{owner}`."
    if row.get("documented_sources") and not row.get("code_read_locations"):
        return "Documented in env examples; no direct literal read was found by the audit."
    if row.get("code_read_locations"):
        return "Discovered by literal env-read audit; not part of the canonical manifest."
    return "Discovered by env audit; review ownership before promoting to public config."


def build_env_catalog_rows(
    repo_root: Path,
    *,
    audit_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, object]]:
    manifest_obj = json.loads(
        (repo_root / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    if audit_rows is None:
        audit_rows = json.loads(
            (repo_root / "docs/configuration/flow-steward-env-audit.json").read_text(
                encoding="utf-8"
            )
        )
    manifest_rows = manifest_obj["variables"]
    manifest_map = {row["name"]: row for row in manifest_rows}
    group_metadata = {
        str(name): dict(meta or {}) for name, meta in dict(manifest_obj.get("groups") or {}).items()
    }

    example_map = {name: _read_env_names(repo_root / name) for name in EXAMPLE_FILES}
    contexts_by_name = _example_contexts(repo_root)

    rows: list[dict[str, object]] = []
    for row in audit_rows:
        name = row["variable_name"]
        manifest = manifest_map.get(name, {})
        locations = row.get("code_read_locations") or []
        rows.append(
            {
                "name": name,
                "canonical_name": row.get("canonical_name", ""),
                "scope": _scope(row, manifest_map),
                "owner": manifest.get("owner", row.get("owning_subsystem", "")),
                "groups": ", ".join(manifest.get("groups", row.get("conditional_groups", []))),
                "environments": ", ".join(
                    manifest.get("environments", row.get("environments", []))
                ),
                "required": manifest.get("doctor_required", row.get("required", False)),
                "secret": manifest.get("secret", row.get("secret", False)),
                "installer_managed": manifest.get("installer_managed", False),
                "default": manifest.get("default", row.get("default_example_value", "")),
                "example_files": ", ".join(
                    fname for fname, envs in example_map.items() if name in envs
                ),
                "example_context": "; ".join(contexts_by_name.get(name, ())),
                "documented_sources": ", ".join(row.get("documented_sources", [])),
                "usage_areas": _usage_areas(locations),
                "code_read_locations": ", ".join(locations),
                "locations_preview": _locations_preview(locations),
                "retired_aliases": ", ".join(row.get("retired_alias_names", [])),
                "notes": manifest.get("notes", row.get("notes", "")),
                "description": _description(
                    row=row,
                    manifest=manifest,
                    group_metadata=group_metadata,
                    contexts=contexts_by_name.get(name, ()),
                ),
                "read_class": _read_class(row.get("code_read_locations") or []),
            }
        )
    rows.sort(key=lambda item: (str(item["scope"]), str(item["name"])))
    return rows


def _write_catalog_csv(rows: list[dict[str, object]], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def render_env_catalog_markdown(
    repo_root: Path,
    *,
    audit_rows: list[dict[str, Any]] | None = None,
) -> str:
    rows = build_env_catalog_rows(repo_root, audit_rows=audit_rows)
    manifest_obj = json.loads(
        (repo_root / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_rows = manifest_obj["variables"]
    manifest_map = {row["name"]: row for row in manifest_rows}
    group_metadata = {
        str(name): dict(meta or {}) for name, meta in dict(manifest_obj.get("groups") or {}).items()
    }
    example_map = {name: _read_env_names(repo_root / name) for name in EXAMPLE_FILES}
    all_example_names = set().union(*example_map.values())
    catalog_names = set(manifest_map) | all_example_names
    catalog_rows = [r for r in rows if str(r["name"]) in catalog_names]
    audit_only_rows = [r for r in rows if str(r["name"]) not in catalog_names]
    counter = Counter(str(r["scope"]) for r in rows)
    catalog_counter = Counter(str(r["scope"]) for r in catalog_rows)
    audit_only_counter = Counter(str(r["scope"]) for r in audit_only_rows)
    public_rows = [r for r in catalog_rows if r["scope"] == "public_operator"]
    public_no_reads = [r for r in public_rows if r["read_class"] == "no_reads"]
    public_tests_only = [
        r for r in public_rows if r["read_class"] in {"tests_only", "tests_docs_only"}
    ]
    public_missing_examples = [r for r in public_rows if not r["example_files"]]
    audit_map = {str(row["name"]): row for row in rows}
    example_only_names = sorted(all_example_names - set(manifest_map))
    example_only_scope_counter = Counter(
        str(audit_map.get(name, {}).get("scope") or "uncategorized") for name in example_only_names
    )
    example_only_file_counts = {
        filename: len(names - set(manifest_map)) for filename, names in example_map.items()
    }

    group_counter: Counter[str] = Counter()
    for item in manifest_rows:
        group_counter.update(str(group) for group in item.get("groups", []))

    md: list[str] = []
    md.append("# Flow Steward Environment Catalog\n")
    md.append(
        "Generated inventory of documented and canonical environment variables. "
        "This is the operator-facing catalog: it includes variables from shipped `.env.example*` overlays plus variables in the canonical env manifest. "
        "The raw scanner also finds internal shell/test handoff names; those are summarized under audit-only findings instead of being promoted into the user-facing catalog.\n\n"
    )
    md.append("Source of truth inputs:\n")
    md.append("- `core/infrastructure/config/flow_steward_env_manifest.json`\n")
    md.append("- `docs/configuration/flow-steward-env-audit.json`\n")
    md.append(
        "- shipped overlays: `.env.example`, `.env.example.prod`, `.env.example.ops`, `.env.example.memory`, `.env.example.test`, `.env.example.quality`\n\n"
    )
    md.append("Regenerate with:\n\n")
    md.append("```bash\n")
    md.append("python -m core.application.tooling.ops.generate_env_catalog\n")
    md.append("```\n\n")
    md.append("## Scope Legend\n")
    md.append("- `Public / Operator`: supported runtime/operator env contracts.\n")
    md.append(
        "- `Test Config`: documented test and E2E configuration, intentionally separate from operator runtime config.\n"
    )
    md.append("- `Internal Bridge`: process-to-process handoff env.\n")
    md.append(
        "- `Internal Runtime`: internal runtime toggles/helpers not intended as normal operator config.\n"
    )
    md.append("- `Script Internal`: shell-script local env surfaces.\n")
    md.append("- `Test Only`: test harness variables not part of the canonical manifest.\n")
    md.append("- `Tooling Internal`: quality/tooling-specific variables.\n\n")
    md.append("## Logical Groups\n")
    md.append("| Group | Description | Manifest Variables |\n|---|---|---|\n")
    for group_name in sorted(group_metadata):
        meta = group_metadata[group_name]
        label = str(meta.get("label") or group_name)
        description = str(meta.get("description") or "-")
        md.append(
            f"| `{_md_cell(label)}` | {_md_cell(description)} | `{group_counter[group_name]}` |\n"
        )
    md.append("\n")
    md.append("## Summary\n")
    md.append(f"- Raw env-like names discovered by audit: `{len(rows)}`\n")
    md.append(f"- Cataloged variables from `.env.example*` or manifest: `{len(catalog_rows)}`\n")
    md.append(f"- Raw audit-only names outside examples/manifest: `{len(audit_only_rows)}`\n")
    md.append(f"- Unique variables in shipped `.env.example*` files: `{len(all_example_names)}`\n")
    md.append(f"- Variables in canonical manifest: `{len(manifest_map)}`\n")
    md.append("\nCataloged variables by scope:\n")
    for key in sorted(catalog_counter):
        md.append(f"- {SCOPE_LABELS.get(key, key)}: `{catalog_counter[key]}`\n")
    md.append("\nRaw audit-only names by scope:\n")
    for key in sorted(counter):
        if audit_only_counter[key]:
            md.append(f"- {SCOPE_LABELS.get(key, key)}: `{audit_only_counter[key]}`\n")
    md.append("\n")
    md.append("## Review Candidates\n")
    md.append(
        "These sections are the first places to review for removal, consolidation, or reclassification.\n\n"
    )
    md.append("### Public Variables With No Direct Runtime Reads\n")
    if public_no_reads:
        md.append(
            f"`{len(public_no_reads)}` cataloged public/operator variables have no direct literal read in the broad audit. "
            "Some are consumed indirectly through registry/config helpers; review the full inventory before removing them.\n\n"
        )
        md.append(
            "| Variable | Owner | Groups | In Example Files | Notes |\n|---|---|---|---|---|\n"
        )
        for r in public_no_reads[:25]:
            md.append(
                f"| `{_md_cell(r['name'])}` | {_md_cell(r['owner'])} | {_md_cell(r['groups'])} | {_md_cell(r['example_files'])} | {_md_cell(r['description'])} |\n"
            )
        if len(public_no_reads) > 25:
            md.append(
                f"\nShowing first 25; `{len(public_no_reads) - 25}` more are in the full inventory below.\n"
            )
    else:
        md.append("None.\n")
    md.append("\n")
    md.append("### Public Variables Used Only In Tests / Docs\n")
    if public_tests_only:
        md.append(
            f"`{len(public_tests_only)}` cataloged public/operator variables are used only in tests/docs according to literal-read scanning.\n\n"
        )
        md.append(
            "| Variable | Owner | Groups | In Example Files | Notes |\n|---|---|---|---|---|\n"
        )
        for r in public_tests_only[:25]:
            md.append(
                f"| `{_md_cell(r['name'])}` | {_md_cell(r['owner'])} | {_md_cell(r['groups'])} | {_md_cell(r['example_files'])} | {_md_cell(r['description'])} |\n"
            )
        if len(public_tests_only) > 25:
            md.append(
                f"\nShowing first 25; `{len(public_tests_only) - 25}` more are in the full inventory below.\n"
            )
    else:
        md.append("None.\n")
    md.append("\n")
    md.append("### Example Variables Not Present In The Canonical Manifest\n")
    md.append(
        "These variables are published in `.env.example*` overlays but are not registry-managed by `flow_steward_env_manifest.json`. "
        "They are included in the full catalog below; this summary highlights where manifest coverage may be incomplete.\n\n"
    )
    md.append("| Example File | Count Missing From Manifest |\n|---|---|\n")
    for filename, count in example_only_file_counts.items():
        if count:
            md.append(f"| `{filename}` | `{count}` |\n")
    md.append("\n| Scope | Count |\n|---|---|\n")
    for scope_name, count in sorted(example_only_scope_counter.items()):
        md.append(f"| {SCOPE_LABELS.get(scope_name, scope_name)} | `{count}` |\n")
    md.append("\n")
    md.append("## Public Variables Missing From Shipped `.env.example*`\n")
    if public_missing_examples:
        md.append("| Variable | Owner | Groups | Notes |\n|---|---|---|---|\n")
        for r in public_missing_examples:
            md.append(
                f"| `{_md_cell(r['name'])}` | {_md_cell(r['owner'])} | {_md_cell(r['groups'])} | {_md_cell(r['description'])} |\n"
            )
    else:
        md.append(
            "All public/operator variables are represented in at least one shipped example file.\n"
        )
    md.append("\n")
    md.append("## Cataloged Inventory By Scope And Owner\n")
    md.append(
        "The table below includes every variable that is either shipped in an `.env.example*` overlay or present in `flow_steward_env_manifest.json`. "
        "`Usage Areas` groups line-level reads by service or repository area; use `docs/configuration/flow-steward-env-audit.json` for exact locations.\n\n"
    )
    for scope_name in [
        "public_operator",
        "test_config",
        "internal_bridge",
        "internal_runtime",
        "script_internal",
        "test_only",
        "tooling_internal",
        "uncategorized",
    ]:
        scope_rows = [r for r in catalog_rows if r["scope"] == scope_name]
        if not scope_rows:
            continue
        md.append(f"### {SCOPE_LABELS[scope_name]}\n")
        by_owner: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in scope_rows:
            by_owner[str(row["owner"] or "unowned")].append(row)
        for owner in sorted(by_owner):
            owner_rows = by_owner[owner]
            md.append(f"#### {_md_cell(owner)}\n")
            md.append(
                "| Variable | Groups | Environments | Required | Secret | Default / Example | Documented In | Usage Areas | Description | First Read Locations |\n"
                "|---|---|---|---|---|---|---|---|---|---|\n"
            )
            for r in owner_rows:
                md.append(
                    f"| `{_md_cell(r['name'])}` | {_md_cell(r['groups'])} | {_md_cell(r['environments'])} | {_yes_no(r['required'])} | {_yes_no(r['secret'])} | {_code_or_dash(r['default'])} | {_md_cell(r['example_files'])} | {_md_cell(r['usage_areas'])} | {_md_cell(r['description'])} | {_md_cell(r['locations_preview'])} |\n"
                )
        md.append("\n")

    md.append("## Raw Audit-Only Findings\n")
    md.append(
        "These names were found by broad env-read scanning, but they are not in shipped `.env.example*` overlays and not in the canonical manifest. "
        "Most are test harness variables, shell-local variables, CI/action locals, or internal process handoff names. "
        "Do not treat this section as operator configuration without first promoting a variable into the manifest or an env example file.\n\n"
    )
    md.append("| Scope | Count | Common Usage Areas |\n|---|---|---|\n")
    for scope_name in sorted(audit_only_counter):
        scope_rows = [r for r in audit_only_rows if r["scope"] == scope_name]
        usage_counter: Counter[str] = Counter()
        for row in scope_rows:
            locations = str(row.get("code_read_locations") or "")
            for location in [item.strip() for item in locations.split(",") if item.strip()]:
                usage_counter[_usage_label(location)] += 1
        usage = ", ".join(f"{label} ({count})" for label, count in usage_counter.most_common(5))
        md.append(
            f"| {SCOPE_LABELS.get(scope_name, scope_name)} | `{audit_only_counter[scope_name]}` | {_md_cell(usage)} |\n"
        )
    return "".join(md).rstrip() + "\n"


def generate_env_catalog(repo_root: Path) -> tuple[Path, Path]:
    write_env_audit_artifact(repo_root=repo_root)
    rows = build_env_catalog_rows(repo_root)

    csv_path = repo_root / "logs/configuration/flow-steward-env-catalog.csv"
    _write_catalog_csv(rows, csv_path)

    md_path = repo_root / "docs/configuration/flow-steward-env-catalog.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_env_catalog_markdown(repo_root)
    md_path.write_text("".join(md), encoding="utf-8")
    return md_path, csv_path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    md_path, csv_path = generate_env_catalog(repo_root)
    print(md_path)
    print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
