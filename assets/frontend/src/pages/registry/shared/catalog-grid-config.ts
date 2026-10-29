import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { appHref } from "@/lib/routing/app-href";

import { normalizeString, type GridEntityRow } from "./catalog-surface-helpers";

function uniqueFieldValues(rows: GridEntityRow[], key: string): string[] {
  return Array.from(
    new Set(
      rows
        .map((row) => normalizeString(row[key]))
        .filter((value) => Boolean(value) && value !== "—"),
    ),
  ).sort((a, b) => a.localeCompare(b));
}

function selectOptions(values: string[], includeNone = false, t: I18nContextValue["t"] = fallbackContext.t): Array<{ value: string; label: string }> {
  const options = values.map((value) => ({ value, label: value }));
  if (includeNone) {
    return [{ value: "none", label: t("registry.published_blocks.none", "none") }, ...options];
  }
  return options;
}

function writableRowActions(
  readOnly: boolean,
  actions: Array<Record<string, unknown>>,
): Array<Record<string, unknown>> {
  return readOnly ? [] : actions;
}

export function buildGridConfig(
  endpoint: string,
  idKey: string,
  rows: GridEntityRow[],
  listPath: string,
  readOnly: boolean, t: I18nContextValue["t"] = fallbackContext.t
): Record<string, unknown> {
  const nativeListPath = appHref(listPath);
  switch (endpoint) {
    case "/api/agents":
    return {
      id_field: "agent_id",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_agents", "Filter agents...") },
      filters_schema: {
        fields: [
          {
            filter_id: "_type_label",
            label: t("trash.columns.type", "Type"),
            widget: "select",
            props: {
              options: uniqueFieldValues(rows, "_type_label").map((value) => ({ value, label: value })),
            },
          },
          {
            filter_id: "_contract",
            label: t("react.registry.contract", "Contract"),
            widget: "select",
            props: {
              options: [
                { value: "ok", label: t("react.registry.ok", "OK") },
                { value: "error", label: t("react.registry.needs_attention", "Needs attention") },
              ],
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: { title: t("react.registry.no_agents_found", "No agents found"), description: t("react.registry.create_one_using_the_button_above", "Create one using the button above.") },
      columns: [
        { key: "display_name", label: t("react.registry.agent", "Agent"), type: "link", link_href: `${nativeListPath}/{agent_id}` },
        { key: "_type_label", label: t("trash.columns.type", "Type"), type: "text" },
        { key: "_caps_text", label: t("react.registry.skills", "Skills"), type: "text", truncate: true, truncate_width: "16rem" },
        { key: "_tools_count", label: t("react.registry.tools", "Tools"), type: "text", width: "110px" },
        { key: "_model", label: t("registry.agents.field.model", "Model"), type: "text", width: "160px", truncate: true, truncate_width: "10rem" },
        {
          key: "_contract",
          label: t("react.registry.contract", "Contract"),
          type: "badge",
          badge_map: { loading: "neutral", ok: "green", error: "red" },
        },
        {
          key: "_enabled_label",
          label: t("extensions.runtime.status", "Status"),
          type: "badge",
          badge_map: { enabled: "green", disabled: "neutral" },
        },
      ],
      row_actions: writableRowActions(readOnly, [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "clone", label: t("react.registry.clone", "Clone"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "enabled", eq: false } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "enabled", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ]),
    };
    case "/api/tools":
    return {
      id_field: "tool_id",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_tools", "Filter tools...") },
      filters_schema: {
        fields: [
          {
            filter_id: "_category",
            label: t("trash.columns.category", "Category"),
            widget: "select",
            props: {
              options: uniqueFieldValues(rows, "_category").map((value) => ({ value, label: value })),
            },
          },
          {
            filter_id: "_impl_type",
            label: t("registry.tools.field.implementation", "Implementation"),
            widget: "select",
            props: {
              options: uniqueFieldValues(rows, "_impl_type").map((value) => ({ value, label: value })),
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
                { value: "unavailable", label: t("monitoring.health.unavailable", "Unavailable") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: { title: t("react.registry.no_tools_found", "No tools found"), description: t("react.registry.create_one_using_the_button_above", "Create one using the button above.") },
      columns: [
        { key: "display_name", label: t("react.registry.tool", "Tool"), type: "link", link_href: `${nativeListPath}/{tool_id}` },
        { key: "_category", label: t("trash.columns.category", "Category"), type: "badge" },
        { key: "_impl_type", label: t("registry.tools.field.implementation", "Implementation"), type: "badge" },
        { key: "_enabled_label", label: t("extensions.runtime.status", "Status"), type: "badge", badge_map: { enabled: "green", disabled: "neutral", unavailable: "red" } },
      ],
      row_actions: writableRowActions(readOnly, [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "clone", label: t("react.registry.clone", "Clone"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "_enable_action_allowed", eq: true } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "_disable_action_allowed", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ]),
    };
    case "/api/skills":
    return {
      id_field: "skill_id",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_skills", "Filter skills...") },
      filters_schema: {
        fields: [
          {
            filter_id: "_category",
            label: t("trash.columns.category", "Category"),
            widget: "select",
            props: {
              options: uniqueFieldValues(rows, "_category").map((value) => ({ value, label: value })),
            },
          },
          {
            filter_id: "_contract",
            label: t("react.registry.contract", "Contract"),
            widget: "select",
            props: {
              options: [
                { value: "ok", label: t("react.registry.ok", "OK") },
                { value: "error", label: t("react.registry.needs_attention", "Needs attention") },
              ],
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: { title: t("react.registry.no_skills_found", "No skills found"), description: t("react.registry.create_one_using_the_button_above", "Create one using the button above.") },
      columns: [
        { key: "display_name", label: t("trash.type.registry_skill", "Skill"), type: "link", link_href: `${nativeListPath}/{skill_id}` },
        { key: "skill_id", label: "ID", type: "id", width: "180px" },
        { key: "_category", label: t("trash.columns.category", "Category"), type: "badge", width: "130px" },
        { key: "description_preview", label: t("registry.workflows.simulation.description", "Description"), type: "text", truncate: true, truncate_width: "24rem" },
        { key: "_instruction_count", label: t("registry.skills.field.instructions_short", "Instructions"), type: "number", width: "110px" },
        { key: "_contract", label: t("react.registry.contract", "Contract"), type: "badge", badge_map: { loading: "neutral", ok: "green", error: "red" } },
        { key: "_enabled_label", label: t("extensions.runtime.status", "Status"), type: "badge", badge_map: { enabled: "green", disabled: "neutral" } },
      ],
      row_actions: writableRowActions(readOnly, [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "clone", label: t("react.registry.clone", "Clone"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "enabled", eq: false } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "enabled", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ]),
    };
    case "/api/guardrails":
    return {
      id_field: "id",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_guardrails", "Filter guardrails...") },
      filters_schema: {
        fields: [
          {
            filter_id: "mode",
            label: t("registry.workflow_detail.run_lab.test_run.mode", "Mode"),
            widget: "select",
            props: {
              options: [
                { value: "deterministic", label: t("react.registry.deterministic", "Deterministic") },
                { value: "semantic", label: t("react.registry.semantic", "Semantic") },
              ],
            },
          },
          {
            filter_id: "severity",
            label: t("security_overview.columns.severity", "Severity"),
            widget: "select",
            props: {
              options: [
                { value: "error", label: t("react.registry.error", "Error") },
                { value: "warning", label: t("react.registry.warning", "Warning") },
              ],
            },
          },
          {
            filter_id: "_scope_kind",
            label: t("react.registry.scope", "Scope"),
            widget: "select",
            props: {
              options: [
                { value: "global", label: t("react.registry.global", "Global") },
                { value: "project", label: t("trash.type.project", "Project") },
              ],
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: {
        title: t("react.registry.no_guardrails_found", "No guardrails found"),
        description: t("react.registry.create_your_first_guardrail_rule_to_start_validating_agent_outputs", "Create your first guardrail rule to start validating agent outputs."),
      },
      columns: [
        { key: "display_name", label: t("react.registry.rule", "Rule"), type: "link", link_href: `${nativeListPath}/{id}`, sortable: true },
        { key: "rule_type", label: t("trash.columns.type", "Type"), type: "text", width: "140px" },
        { key: "mode", label: t("registry.workflow_detail.run_lab.test_run.mode", "Mode"), type: "badge", width: "130px", badge_map: { deterministic: "accent", semantic: "amber" } },
        { key: "severity", label: t("security_overview.columns.severity", "Severity"), type: "badge", width: "120px", badge_map: { error: "red", warning: "amber" } },
        { key: "_scope_label", label: t("react.registry.scope", "Scope"), type: "text", truncate: true, truncate_width: "16rem" },
        { key: "_agents_text", label: t("react.registry.agents_title", "Agents"), type: "text", truncate: true, truncate_width: "16rem" },
        { key: "description", label: t("registry.workflows.simulation.description", "Description"), type: "text", truncate: true, truncate_width: "24rem" },
        { key: "_enabled_label", label: t("extensions.runtime.status", "Status"), type: "badge", width: "110px", badge_map: { enabled: "green", disabled: "neutral" } },
      ],
      row_actions: readOnly
        ? []
        : [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "enabled", eq: false } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "enabled", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ],
    };
    case "/api/data-contracts":
    return {
      id_field: "contract_id",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_contracts", "Filter contracts...") },
      filters_schema: {
        fields: [
          {
            filter_id: "kind",
            label: t("react.registry.kind", "Kind"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "kind"), undefined, t),
            },
          },
          {
            filter_id: "_projection_label",
            label: t("react.registry.projection", "Projection"),
            widget: "select",
            props: {
              options: [
                { value: "derived", label: t("react.registry.derived", "derived") },
                { value: "manual", label: t("react.registry.manual", "manual") },
              ],
            },
          },
          {
            filter_id: "_sync_label",
            label: t("react.registry.sync", "Sync"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "_sync_label"), undefined, t),
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: {
        title: t("react.registry.no_data_contracts_available", "No data contracts available"),
        description: t("react.registry.contracts_appear_here_after_workflow_and_capability_schemas_are_published", "Contracts appear here after workflow and capability schemas are published."),
      },
      columns: [
        { key: "contract_id", label: t("react.registry.contract_id", "Contract ID"), type: "id", width: "170px" },
        {
          key: "display_name",
          label: t("users.modal.display_name", "Display Name"),
          type: "link",
          link_href: `${nativeListPath}/{contract_id}`,
          truncate: true,
          truncate_width: "18rem",
        },
        { key: "kind", label: t("react.registry.kind", "Kind"), type: "badge", badge_map: { json: "accent", list: "amber" } },
        { key: "version", label: t("registry.data_contracts.column.version", "Version"), type: "text", width: "110px" },
        { key: "_schema_summary", label: t("registry.data_contracts.column.schema", "Schema"), type: "text", truncate: true, truncate_width: "14rem" },
        { key: "_tags_summary", label: t("registry.tools.field.tags", "Tags"), type: "text", truncate: true, truncate_width: "14rem" },
        {
          key: "_projection_label",
          label: t("react.registry.projection", "Projection"),
          type: "badge",
          badge_map: { derived: "amber", manual: "neutral" },
        },
        { key: "_sync_label", label: t("react.registry.sync", "Sync"), type: "text", width: "120px" },
        {
          key: "_enabled_label",
          label: t("extensions.runtime.status", "Status"),
          type: "badge",
          badge_map: { enabled: "green", disabled: "neutral" },
        },
      ],
      row_actions: readOnly
        ? []
        : [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "enabled", eq: false } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "enabled", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ],
    };
    case "/api/fields":
    return {
      id_field: "field_key",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_fields", "Filter fields...") },
      filters_schema: {
        fields: [
          {
            filter_id: "value_type",
            label: t("trash.columns.type", "Type"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "value_type"), undefined, t),
            },
          },
          {
            filter_id: "_contract_label",
            label: t("react.registry.contract", "Contract"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "_contract_label"), true, t),
            },
          },
          {
            filter_id: "_source_label",
            label: t("react.registry.source_variant_0e570ca6", "Source"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "_source_label"), undefined, t),
            },
          },
          {
            filter_id: "_projection_label",
            label: t("react.registry.projection", "Projection"),
            widget: "select",
            props: {
              options: [
                { value: "derived", label: t("react.registry.derived", "derived") },
                { value: "manual", label: t("react.registry.manual", "manual") },
              ],
            },
          },
          {
            filter_id: "_sync_label",
            label: t("react.registry.sync", "Sync"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "_sync_label"), undefined, t),
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: {
        title: t("react.registry.no_fields_available", "No fields available"),
        description: t("react.registry.field_definitions_appear_here_after_registry_contracts_are_synced", "Field definitions appear here after registry contracts are synced."),
      },
      columns: [
        { key: "field_key", label: t("react.registry.field_key", "Field Key"), type: "id", width: "170px" },
        {
          key: "display_name",
          label: t("users.modal.display_name", "Display Name"),
          type: "link",
          link_href: `${nativeListPath}/{field_key}`,
          truncate: true,
          truncate_width: "16rem",
        },
        {
          key: "value_type",
          label: t("trash.columns.type", "Type"),
          type: "badge",
          badge_map: { text: "accent", json: "amber", list: "amber", number: "neutral", boolean: "neutral" },
        },
        { key: "_contract_label", label: t("react.registry.contract", "Contract"), type: "text", truncate: true, truncate_width: "14rem" },
        { key: "_source_label", label: t("react.registry.source_variant_0e570ca6", "Source"), type: "text", width: "130px" },
        {
          key: "_projection_label",
          label: t("react.registry.projection", "Projection"),
          type: "badge",
          badge_map: { derived: "amber", manual: "neutral" },
        },
        { key: "_sync_label", label: t("react.registry.sync", "Sync"), type: "text", width: "110px" },
        {
          key: "_manual_label",
          label: t("react.registry.manual_mapping", "Manual Mapping"),
          type: "badge",
          badge_map: { allowed: "green", blocked: "neutral" },
        },
        {
          key: "_enabled_label",
          label: t("extensions.runtime.status", "Status"),
          type: "badge",
          badge_map: { enabled: "green", disabled: "neutral" },
        },
      ],
      row_actions: readOnly
        ? []
        : [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "enabled", eq: false } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "enabled", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ],
    };
    case "/api/published-blocks":
    return {
      id_field: "block_key",
      title: "",
      search: { enabled: true, placeholder: t("react.registry.filter_published_blocks", "Filter published blocks...") },
      filters_schema: {
        fields: [
          {
            filter_id: "block_type",
            label: t("react.registry.block_type", "Block Type"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "block_type"), undefined, t),
            },
          },
          {
            filter_id: "source_kind",
            label: t("react.registry.source_variant_0e570ca6", "Source"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "source_kind"), undefined, t),
            },
          },
          {
            filter_id: "_family_label",
            label: t("react.registry.family", "Family"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "_family_label"), true, t),
            },
          },
          {
            filter_id: "_projection_label",
            label: t("react.registry.projection", "Projection"),
            widget: "select",
            props: {
              options: [
                { value: "derived", label: t("react.registry.derived", "derived") },
                { value: "manual", label: t("react.registry.manual", "manual") },
              ],
            },
          },
          {
            filter_id: "_sync_label",
            label: t("react.registry.sync", "Sync"),
            widget: "select",
            props: {
              options: selectOptions(uniqueFieldValues(rows, "_sync_label"), undefined, t),
            },
          },
          {
            filter_id: "_enabled_label",
            label: t("extensions.runtime.status", "Status"),
            widget: "select",
            props: {
              options: [
                { value: "enabled", label: t("settings.mcp.status.enabled", "Enabled") },
                { value: "disabled", label: t("react.registry.disabled", "Disabled") },
              ],
            },
          },
        ],
      },
      pagination: { page_size_default: 25, page_size_options: [25, 50, 100, 200] },
      empty_state: {
        title: t("react.registry.no_published_blocks_available", "No published blocks available"),
        description: t("react.registry.published_block_contracts_appear_here_after_registry_publishing_completes", "Published block contracts appear here after registry publishing completes."),
      },
      columns: [
        {
          key: "block_key",
          label: t("react.registry.block_key", "Block Key"),
          type: "link",
          link_href: `${nativeListPath}/{block_key}`,
          truncate: true,
          truncate_width: "18rem",
        },
        { key: "display_name", label: t("users.modal.display_name", "Display Name"), type: "text", truncate: true, truncate_width: "16rem" },
        {
          key: "block_type",
          label: t("react.registry.block_type", "Block Type"),
          type: "badge",
          badge_map: { agent: "accent", connector: "amber", workflow: "neutral" },
        },
        {
          key: "source_kind",
          label: t("react.registry.source_variant_0e570ca6", "Source"),
          type: "badge",
          badge_map: { agent: "accent", connector: "amber", workflow: "neutral" },
        },
        { key: "source_ref", label: t("react.registry.source_ref", "Source Ref"), type: "id", width: "200px" },
        { key: "_family_label", label: t("react.registry.family", "Family"), type: "text", width: "130px" },
        { key: "_io_summary", label: t("react.registry.contract_i_o", "Contract I/O"), type: "text", width: "130px" },
        {
          key: "_projection_label",
          label: t("react.registry.projection", "Projection"),
          type: "badge",
          badge_map: { derived: "amber", manual: "neutral" },
        },
        { key: "_sync_label", label: t("react.registry.sync", "Sync"), type: "text", width: "110px" },
        {
          key: "_enabled_label",
          label: t("extensions.runtime.status", "Status"),
          type: "badge",
          badge_map: { enabled: "green", disabled: "neutral" },
        },
      ],
      row_actions: readOnly
        ? []
        : [
            { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
            { action_id: "toggle-on", label: t("extensions.detail.enable", "Enable"), variant: "ghost", visible_if: { field: "enabled", eq: false } },
            { action_id: "toggle-off", label: t("extensions.detail.disable", "Disable"), variant: "ghost", visible_if: { field: "enabled", eq: true } },
            { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
          ],
    };
    default:
      return {
    id_field: "_entity_id",
    title: "",
    search: { enabled: true, placeholder: t("react.registry.search_catalog", "Search catalog...") },
    pagination: { page_size_default: 25, page_size_options: [25, 50, 100] },
    empty_state: {
      title: t("react.registry.no_entries_found", "No entries found"),
      description: t("react.registry.try_adjusting_your_search_or_filters", "Try adjusting your search or filters."),
    },
    columns: [
      { key: "_entity_id", label: idKey, type: "text", width: "180px" },
      { key: "_entity_name", label: t("settings.mcp.fields.name", "Name"), type: "text", truncate: true },
      { key: "_entity_description", label: t("registry.workflows.simulation.description", "Description"), type: "text", truncate: true },
      { key: "_enabled_label", label: t("extensions.runtime.status", "Status"), type: "badge", width: "100px", badge_map: { enabled: "green", disabled: "neutral" } },
    ],
    row_actions: readOnly
      ? []
      : [
          { action_id: "edit", label: t("extensions.repeater.edit", "Edit"), variant: "ghost" },
          { action_id: "toggle", label: t("react.registry.toggle", "Toggle"), variant: "ghost" },
          { action_id: "delete", label: t("settings.model_providers.delete", "Delete"), variant: "danger" },
        ],
      };
  }
}
