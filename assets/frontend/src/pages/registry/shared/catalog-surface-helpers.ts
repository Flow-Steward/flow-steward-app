import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type RegistryEntity = Record<string, unknown>;

export type GridEntityRow = RegistryEntity & {
  _entity_id: string;
  _enabled_label?: string;
  _contract?: "loading" | "ok" | "error";
};

export type EntityForm = {
  description: string;
  display_name: string;
  enabled: boolean;
  identifier: string;
};

export const EMPTY_FORM: EntityForm = {
  description: "",
  display_name: "",
  enabled: false,
  identifier: "",
};

export function objectOrEmpty(value: unknown): RegistryEntity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RegistryEntity) : {};
}

export function normalizeString(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

function normalizeEnabledLabel(enabled: boolean): string {
  return enabled ? "enabled" : "disabled";
}

export function parseLines(value: string): string[] {
  return firstStringFromUnknownValue([value, ""])
    .split("\n")
    .map((line) => line.trim())
    .filter(isTruthy);
}

export function inferProviderFromModel(modelId: string): string {
  const model = normalizeString(modelId).toLowerCase();
  if (!model) {
    return "";
  }
  if (model.startsWith("claude-")) return "anthropic";
  if (model.startsWith("gpt-")) return "openai";
  if (model.startsWith("gemini-")) return "google";
  if (model.startsWith("qwen-")) return "qwen";
  if (model.startsWith("deepseek-")) return "deepseek";
  if (model.startsWith("grok-")) return "xai";
  return "";
}

export function registryListPath(endpoint: string): string {
  return firstStringFromUnknownValue([endpoint, ""]).replace(/^\/api/, "/registry");
}

export function modalIdForEndpoint(endpoint: string): string {
  if (endpoint === "/api/tools") return "modal-create-tool";
  if (endpoint === "/api/skills") return "modal-create-cap";
  if (endpoint === "/api/agents") return "modal-create";
  if (endpoint === "/api/guardrails") return "modal-create-guardrail";
  return "modal-create-entry";
}

function applyGuardrailDefaults(payload: RegistryEntity): void {
  if (!normalizeString(payload.mode)) payload.mode = "deterministic";
  if (!normalizeString(payload.rule_type)) payload.rule_type = "forbidden_patterns";
  if (!normalizeString(payload.severity)) payload.severity = "warning";
  if (!payload.config || typeof payload.config !== "object" || Array.isArray(payload.config)) {
    payload.config = { patterns: [] };
  }
}

function normalizeContractRows(
  value: unknown,
  defaultRequired: boolean,
): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.reduce<Array<Record<string, unknown>>>((rows, item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return rows;
    const row = item as Record<string, unknown>;
    const name = normalizeString(row.name);
    if (!name) return rows;
    rows.push({
      name,
      type: normalizeString(row.type) || "text",
      description: normalizeString(row.description),
      required: row.required === undefined ? defaultRequired : Boolean(row.required),
      contract_id: normalizeString(row.contract_id) || null,
    });
    return rows;
  }, []);
}

function applySkillContractDefaults(payload: RegistryEntity): void {
  const expectedInputs = normalizeContractRows(payload.expected_inputs || payload._inputs, false);
  const expectedOutputs = normalizeContractRows(payload.expected_outputs || payload._outputs, true);
  payload.expected_inputs = expectedInputs;
  payload.expected_outputs = expectedOutputs.length
    ? expectedOutputs
    : [{ name: "result", type: "text", description: "Primary skill output", required: true, contract_id: null }];
}

export function buildPayload(
  form: EntityForm,
  idKey: string,
  existing?: RegistryEntity,
  endpoint = ""
): RegistryEntity {
  const base = existing ? { ...existing } : {};
  const payload: RegistryEntity = { ...base };

  const identifier = form.identifier.trim();
  if (identifier) {
    payload[idKey] = identifier;
  }
  if (form.display_name.trim()) {
    payload.display_name = form.display_name.trim();
    if (!firstStringFromUnknownValue([payload.name, ""]).trim()) {
      payload.name = form.display_name.trim();
    }
  }
  payload.description = form.description.trim();
  payload.enabled = form.enabled;

  if (endpoint === "/api/guardrails") {
    applyGuardrailDefaults(payload);
  }

  if (endpoint === "/api/skills") {
    applySkillContractDefaults(payload);
  }

  return payload;
}

export function buildGridRows(
  endpoint: string,
  idKey: string,
  items: RegistryEntity[],
  t: I18nContextValue["t"] = fallbackContext.t,
  categoryLabel: (id: string) => string = (id) => id,
): GridEntityRow[] {
  const categoryCell = (item: RegistryEntity) => {
    const category = normalizeString(item.category);
    return category ? categoryLabel(category) : "—";
  };
  if (endpoint === "/api/agents") {
    return items.map((item) => {
      const skills = Array.isArray(item._skills) ? item._skills : [];
      const tools = Array.isArray(item._tools) ? item._tools : [];
      const toolCount = Number(item.tool_count || tools.length);
      const skillNames = normalizeString(item.skill_names);
      const skillsLabel = skillNames || skills
        .map((skill) => normalizeString((skill as RegistryEntity).display_name || (skill as RegistryEntity).skill_id))
        .filter(isTruthy)
        .join(", ") || t("registry.published_blocks.none", "none");
      const agentId = normalizeString(item.agent_id || item.id);
      return {
        ...item,
        _entity_id: agentId,
        _category: categoryCell(item),
        _caps_text: skillsLabel,
        _tools_count: t("react.registry.display_agent_tool_count", "{count} tools", { count: toolCount }),
        _model: normalizeString(item.model || objectOrEmpty(item.config).model) || "—",
        _contract: "loading",
        _enabled_label: item.enabled ? "enabled" : "disabled",
      };
    });
  }

  if (endpoint === "/api/tools") {
    return items.map((item) => {
      const toolId = normalizeString(item.tool_id || item.id);
      const implementation = objectOrEmpty(item.implementation);
      const config = objectOrEmpty(item.config);
      const availabilityStatus = normalizeString(item.availability_status).toLowerCase() || "available";
      const availabilityReasonCode = normalizeString(item.availability_reason_code);
      const availabilityReasonText = normalizeString(item.availability_reason_text).slice(0, 240);
      const availabilityReason = availabilityReasonText || availabilityReasonCode || t("react.workflows.available", "Available");
      const enabled = Boolean(item.enabled);
      return {
        ...item,
        _entity_id: toolId,
        description_short:
          normalizeString(item.description_preview) || firstStringFromUnknownValue([item.description, ""]).slice(0, 80),
        _category: categoryCell(item),
        _impl_type: normalizeString(item.impl_type || implementation.type) || "—",
        _cost_tier: normalizeString(item.cost_tier || config.cost_tier) || "—",
        _availability_reason: availabilityReason,
        _enable_action_allowed: !enabled && availabilityStatus === "available",
        _disable_action_allowed: enabled,
        _enabled_label:
          availabilityStatus === "available"
            ? normalizeEnabledLabel(enabled)
            : "unavailable",
      };
    });
  }

  if (endpoint === "/api/skills") {
    return items.map((item) => {
      const skillId = normalizeString(item.skill_id || item.id);
      const instructions = Array.isArray(item.instructions) ? item.instructions : [];
      return {
        ...item,
        _entity_id: skillId,
        description_preview:
          normalizeString(item.description_preview) || normalizeString(item.description),
        _category: categoryCell(item),
        _instruction_count: Number(item.instruction_count || instructions.length || 0),
        _contract: "loading",
        _enabled_label: item.enabled ? "enabled" : "disabled",
      };
    });
  }

  if (endpoint === "/api/guardrails") {
    return items.map((item) => {
      const guardrailId = firstStringFromUnknownValue([item.id, item.guardrail_id, ""]).trim();
      const scopedAgents = Array.isArray(item._agents) ? item._agents : [];
      const projectId = normalizeString(item.project_id);
      return {
        ...item,
        _entity_id: guardrailId,
        _agents_text: scopedAgents.length ? scopedAgents.join(", ") : t("react.registry.display_unassigned", "Unassigned"),
        _scope_kind: projectId ? "project" : "global",
        _scope_label: projectId ? t("react.registry.display_project_scope", "Project {id}", { id: projectId }) : t("react.registry.display_global_scope", "Global"),
        _enabled_label: item.enabled ? "enabled" : "disabled",
      };
    });
  }

  if (endpoint === "/api/data-contracts") {
    return items.map((item) => {
      const contractId = normalizeString(item.contract_id || item.id);
      const schema = objectOrEmpty(item.schema_json || item.schema);
      const schemaProperties = objectOrEmpty(schema.properties);
      const schemaFieldCount = Object.keys(schemaProperties).length;
      return {
        ...item,
        _entity_id: contractId,
        _schema_summary: schemaFieldCount
          ? `${schemaFieldCount} fields`
          : normalizeString(schema.type) || "schema",
        _tags_summary:
          Array.isArray(item.tags) && item.tags.length
            ? item.tags.map((tag) => normalizeString(tag)).filter(isTruthy).join(", ")
            : "none",
        _projection_label: normalizeString(item.projection_mode) === "derived" ? "derived" : "manual",
        _sync_label:
          normalizeString(item.sync_status) || (item.enabled === false ? "disabled" : "active"),
        _enabled_label: item.enabled === false ? "disabled" : "enabled",
      };
    });
  }

  if (endpoint === "/api/fields") {
    return items.map((item) => {
      const fieldKey = normalizeString(item.field_key || item.id);
      return {
        ...item,
        _entity_id: fieldKey,
        _contract_label: normalizeString(item.data_contract_id) || "none",
        _source_label: normalizeString(item.created_from_kind) || "manual",
        _projection_label: normalizeString(item.projection_mode) === "derived" ? "derived" : "manual",
        _sync_label:
          normalizeString(item.sync_status) || (item.enabled === false ? "disabled" : "active"),
        _manual_label: item.manual_allowed === false ? "blocked" : "allowed",
        _enabled_label: item.enabled === false ? "disabled" : "enabled",
      };
    });
  }

  if (endpoint === "/api/published-blocks") {
    return items.map((item) => {
      const blockKey = normalizeString(item.block_key || item.id);
      const inputCount = Number(item._input_count || (Array.isArray(item.inputs) ? item.inputs.length : 0) || 0);
      const outputCount = Number(
        item._output_count || (Array.isArray(item.outputs) ? item.outputs.length : 0) || 0,
      );
      return {
        ...item,
        _entity_id: blockKey,
        _family_label: normalizeString(item.family) || "none",
        _projection_label: normalizeString(item.projection_mode) === "derived" ? "derived" : "manual",
        _sync_label:
          normalizeString(item.sync_status) || (item.enabled === false ? "disabled" : "active"),
        _enabled_label: item.enabled === false ? "disabled" : "enabled",
        _io_summary: `${inputCount} in / ${outputCount} out`,
      };
    });
  }

  return items.map((item) => {
    const entityId = normalizeString(item[idKey] || item.id);
    return {
      ...item,
      _entity_id: entityId,
      _entity_name:
        normalizeString(item.display_name || item.name || item.title || item[idKey] || item.id) || "—",
      _entity_description: normalizeString(item.description || item.summary),
      _enabled_label: item.enabled === false ? "disabled" : "enabled",
    };
  });
}

export async function applyContractValidation(
  endpoint: string,
  rows: GridEntityRow[],
): Promise<GridEntityRow[]> {
  if (endpoint !== "/api/agents" && endpoint !== "/api/skills") {
    return rows;
  }
  const idField = endpoint === "/api/agents" ? "agent_id" : "skill_id";
  const results = await Promise.allSettled(
    rows.map(async (row) => {
      const entityId = normalizeString(row[idField] || row._entity_id);
      if (!entityId) {
        throw new Error("missing_id");
      }
      const payload = await fetchJson<unknown>(
        `${endpoint}/${encodeURIComponent(entityId)}/contract-validation`,
      );
      return objectOrEmpty(payload).validation || payload;
    }),
  );
  return rows.map((row, index) => {
    const outcome = results[index];
    if (outcome.status !== "fulfilled") {
      return { ...row, _contract: "error" };
    }
    const validation = objectOrEmpty(outcome.value);
    const errors = Array.isArray(validation.errors) ? validation.errors : [];
    return {
      ...row,
      _contract: errors.length ? "error" : "ok",
    };
  });
}
