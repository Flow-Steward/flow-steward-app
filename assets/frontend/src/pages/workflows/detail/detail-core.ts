import { fallbackContext, type I18nContextValue, type TranslateParams } from "@/lib/i18n/context";
import type {
  WorkflowCanvasIssue,
  WorkflowForEach,
  WorkflowGroup,
} from "@/pages/workflows/shared/api";
import {
  domIdSegment,
  objectOrEmpty,
} from "@/pages/workflows/detail/detail-interface";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import {
  firstStringFromUnknownValue,
  isTruthy,
  stringFromUnknownValue,
} from "@/lib/utils/unknown-values";

export type WorkflowStepRow = {
  agentId: string;
  agentLabel: string;
  blockKey: string;
  /** Its Group (a step directly in a Group); empty otherwise. */
  groupId: string;
  /** Its For each (a step in a For each body); empty otherwise. */
  forEachId: string;
  laneId: string;
  /** Created by the unapplied change set; it does not exist on the server yet. */
  local: boolean;
  optional: boolean;
  ordinal: number;
  primaryLabel: string;
  raw: Record<string, unknown>;
  /** The authored step_id, the step's identity everywhere. */
  runtimeId: string;
  secondaryLabel: string;
  skillLabel: string;
  skillId: string;
  stepKind: string;
  stepKey: string;
};

/** One container of the definition: a Group (runs once) or a For each (runs per item). */
export type WorkflowGroupRow = {
  bodyKind: "sequence" | "lanes" | "";
  containerKind: "group" | "for_each";
  description: string;
  displayName: string;
  /** The container's business id (a Group or a For each id; ids are unique). */
  groupId: string;
  lanes: string[];
  local: boolean;
  /** For each only: the source list, the item name and the optional index name. */
  over: string;
  itemName: string;
  indexName: string;
  /** For each only: the sequence Group it sits in, or "" at the root. */
  parentGroupId: string;
  raw: WorkflowGroup | WorkflowForEach;
  steps: WorkflowStepRow[];
  /** Group only: the For each containers inside its sequence body. */
  forEachIds: string[];
};

export type WorkflowWebhookTrigger = {
  entrypoint_id?: string | null;
  workflow_id: string;
  entry_key: string;
  enabled: boolean;
  allowed_methods: string[];
  auth_mode?: string;
  auth_config?: Record<string, unknown>;
  source_restrictions?: Record<string, unknown>;
  allowed_content_types?: string[];
  max_body_bytes?: number;
  max_file_bytes?: number;
  max_files?: number;
  idempotency_mode: string;
  response_mode: string;
  input_mapping: Record<string, unknown>;
  description?: string;
  requires_secret: boolean;
  public_path: string;
  public_url: string;
  public_url_source?: string;
  readiness?: {
    status?: string;
    errors?: Array<Record<string, unknown>>;
  };
};

export type ExpectedWebhookInput = {
  name: string;
  required: boolean;
  type: string;
};

export type WorkflowProviderTrigger = {
  trigger_id: string;
  workflow_id?: string;
  project_id?: string | null;
  provider_key: string;
  trigger_type_id?: string;
  display_name?: string;
  description?: string;
  event_name?: string;
  enabled: boolean;
  trigger_config: Record<string, unknown>;
  readiness?: {
    status?: string;
    errors?: Array<Record<string, unknown>>;
  };
  config_schema?: Record<string, unknown>;
  ui_schema?: Record<string, unknown>;
  provider_ingress_url?: string | null;
};

export type WorkflowTriggerCatalogEntry = {
  trigger_family: "incoming_webhook" | "provider";
  catalog_id: string;
  trigger_type_id: string;
  provider_key: string;
  display_name: string;
  description?: string;
  config_schema?: Record<string, unknown>;
  ui_schema?: Record<string, unknown>;
  extension_id?: string | null;
  extension_kind?: string | null;
};

export type TriggerUiField = {
  field_id?: string;
  path?: string;
  name?: string;
  label?: string;
  widget?: string;
  widget_type?: string;
  placeholder?: string;
  options?: Array<{ label?: string; value?: string }>;
};

export type AvailableWorkflowVariable = {
  description?: string;
  description_key?: string;
  description_params?: TranslateParams;
  name?: string;
  step_id?: string;
  group_id?: string | null;
  [key: string]: unknown;
};

export type WorkflowVariableSuggestionOption = {
  description: string;
  label?: string;
  meta: string;
  stepId: string;
  value: string;
  variant: "global" | "workflow" | "state_alias" | "step_artifact";
};

export type WorkflowVariableSuggestionGroup = {
  key: string;
  label: string;
  options: WorkflowVariableSuggestionOption[];
};

export function triggerUiFields(value: unknown): TriggerUiField[] {
  const uiSchema = objectOrEmpty(value);
  const rawFields = Array.isArray(uiSchema.fields) ? uiSchema.fields : [];
  return rawFields
    .map((item) => objectOrEmpty(item) as TriggerUiField)
    .filter((item) => Boolean(triggerUiFieldPath(item)));
}

export function triggerUiFieldPath(field: {
  field_id?: unknown;
  name?: unknown;
  path?: unknown;
}): string {
  return firstStringFromUnknownValue([
    field.path,
    field.field_id,
    field.name,
    "",
  ]).trim();
}

export function triggerUiFieldWidget(field: {
  widget?: unknown;
  widget_type?: unknown;
}): string {
  return firstStringFromUnknownValue([field.widget, field.widget_type, "text"])
    .trim()
    .toLowerCase();
}

export function normalizeAvailableWorkflowVariableSuggestions(
  payload: Record<string, unknown> | undefined,
  t: I18nContextValue["t"] = fallbackContext.t,
  context: { interfaceContract?: Record<string, unknown>; producerLabels?: Record<string, string>;
    producerMembers?: Record<string, AvailableWorkflowVariable[]> } = {},
): WorkflowVariableSuggestionGroup[] {
  const stateVars = Array.isArray(payload?.state)
    ? (payload?.state as AvailableWorkflowVariable[])
    : [];
  const dottedPathVars = Array.isArray(payload?.dotted_path)
    ? (payload?.dotted_path as AvailableWorkflowVariable[])
    : [];
  const priorOutputs = Array.isArray(payload?.prior_outputs)
    ? (payload?.prior_outputs as AvailableWorkflowVariable[])
    : [];
  // The item and index names of the For each around the step.
  const loopVars = Array.isArray(payload?.loop)
    ? (payload?.loop as AvailableWorkflowVariable[])
    : [];
  const groups: WorkflowVariableSuggestionGroup[] = [];
  const seen = new Set<string>();
  const humanize = (value: string) => value.replaceAll(/[_-]+/g, " ").replaceAll(/\b\w/g, (letter) => letter.toUpperCase());
  const addressableName = (name: string) => name.length > 0 && name === name.trim() &&
    ![".", "[", "]"].some((part) => name.includes(part));

  function ensureGroup(key: string, label: string) {
    const existing = groups.find((group) => group.key === key);
    if (existing) {
      return existing;
    }
    const nextGroup: WorkflowVariableSuggestionGroup = {
      key,
      label,
      options: [],
    };
    groups.push(nextGroup);
    return nextGroup;
  }

  function addOption(
    groupKey: string,
    groupLabel: string,
    value: string,
    item: AvailableWorkflowVariable,
    metaOverride = "",
    variant: WorkflowVariableSuggestionOption["variant"] = "workflow",
    labelOverride = "",
  ) {
    const normalizedValue = textOrEmpty(value);
    if (!normalizedValue) {
      return;
    }
    const uniqueKey = `${groupKey}:${normalizedValue}`;
    if (seen.has(uniqueKey)) {
      return;
    }
    seen.add(uniqueKey);
    const description = textOrEmpty(item.description);
    const descriptionKey = textOrEmpty(item.description_key);
    const descriptionParams = Object.fromEntries(
      Object.entries(objectOrEmpty(item.description_params)).filter(
        ([, parameter]) => parameter === null || ["string", "number", "boolean"].includes(typeof parameter),
      ),
    ) as TranslateParams;
    ensureGroup(groupKey, groupLabel).options.push({
      description: descriptionKey ? t(descriptionKey, description, descriptionParams) : description,
      label: labelOverride || textOrEmpty(item.label) || normalizedValue,
      meta:
        metaOverride ||
        [textOrEmpty(item.type), textOrEmpty(item.source)]
          .filter(isTruthy)
          .join(" · "),
      stepId: textOrEmpty(item.step_id),
      value: normalizedValue,
      variant,
    });
  }

  // The applied, matching workflow contract is the authority for named inputs.
  const contract = objectOrEmpty(context.interfaceContract);
  const inputGroupLabel = t("react.workflows.workflow_inputs", "Inputs");
  if (textOrEmpty(contract.input_mode) === "json_schema") {
    const schema = objectOrEmpty(contract.input_schema);
    const properties = objectOrEmpty(schema.properties);
    for (const [name, rawField] of Object.entries(properties)) {
      if (!addressableName(name)) continue;
      const field = objectOrEmpty(rawField);
      if (!["string", "number", "integer", "boolean", "object", "array"].includes(textOrEmpty(field.type)) ||
        ["$ref", "anyOf", "oneOf", "allOf", "if", "then", "else", "patternProperties"].some((key) => key in field)) continue;
      const label = textOrEmpty(field.title) || humanize(name);
      addOption("inputs", inputGroupLabel, `state.inputs.${name}`,
        { name, description: textOrEmpty(field.description), type: textOrEmpty(field.type) },
        textOrEmpty(field.type), "workflow", label);
      if (field.type !== "object") continue;
      for (const [memberName, rawMember] of Object.entries(objectOrEmpty(field.properties))) {
        if (!addressableName(memberName)) continue;
        const member = objectOrEmpty(rawMember);
        if (!["string", "number", "integer", "boolean", "object", "array"].includes(textOrEmpty(member.type)) ||
          ["$ref", "anyOf", "oneOf", "allOf", "if", "then", "else", "patternProperties"].some((key) => key in member)) continue;
        addOption("inputs", inputGroupLabel, `state.inputs.${name}.${memberName}`,
          { name: memberName, description: textOrEmpty(member.description), type: textOrEmpty(member.type) },
          textOrEmpty(member.type), "workflow",
          `${label} → ${textOrEmpty(member.title) || humanize(memberName)}`);
      }
    }
  } else {
    for (const rawField of Array.isArray(contract.expected_inputs) ? contract.expected_inputs : []) {
      const field = objectOrEmpty(rawField);
      const name = textOrEmpty(field.name);
      if (!addressableName(name)) continue;
      addOption("inputs", inputGroupLabel, `state.inputs.${name}`,
        { name, description: textOrEmpty(field.description), type: textOrEmpty(field.type) },
        textOrEmpty(field.type), "workflow", textOrEmpty(field.label) || humanize(name));
    }
  }

  stateVars.forEach((item) => {
    const name = textOrEmpty(item.name);
    if (!name) {
      return;
    }
    addOption(
      "global",
      t("react.workflows.advanced_values", "Advanced values"),
      `state.${name}`,
      item,
      "",
      "global",
    );
  });

  dottedPathVars.forEach((item) => {
    const name = textOrEmpty(item.name);
    if (!name || name.startsWith("state.inputs.") || name.startsWith("inputs.")) {
      return;
    }
    addOption(
      "workflow",
      t("react.workflows.workflow_variables", "Workflow variables"),
      name,
      item,
      t("react.workflows.display_direct_path", "direct path"),
      "workflow",
    );
    addOption(
      "workflow",
      t("react.workflows.workflow_variables", "Workflow variables"),
      `state.${name}`,
      item,
      t("react.workflows.display_state_alias", "state alias"),
      "workflow",
    );
  });

  loopVars.forEach((item) => {
    const name = textOrEmpty(item.name);
    if (!name) {
      return;
    }
    addOption(
      "workflow",
      t("react.workflows.workflow_variables", "Workflow variables"),
      name.startsWith("state.") ? name : `state.${name}`,
      item,
      t("react.workflows.display_for_each_variable", "For each item"),
      "workflow",
    );
  });

  priorOutputs.forEach((item) => {
    const name = textOrEmpty(item.name);
    if (!name) {
      return;
    }
    const stepId = textOrEmpty(item.step_id);
    addOption(
      "step",
      t("react.workflows.display_step_variables", "Step variables"),
      `state.${name}`,
      item,
      stepId ? t("react.workflows.display_state_alias_step", "state alias · from {step}", { step: stepId }) : t("react.workflows.display_state_alias", "state alias"),
      "state_alias",
      `${textOrEmpty(context.producerLabels?.[stepId]) || humanize(stepId)} → ${textOrEmpty(item.label) || humanize(name)}`,
    );
    // The backend's prior_outputs list decides whether this saved producer is in scope.
    // A member is offered only when its shape is declared by that exact producer.
    if (stepId && addressableName(name)) {
      for (const member of context.producerMembers?.[`${stepId}:${name}`] || []) {
        const memberName = textOrEmpty(member.name);
        if (!addressableName(memberName)) continue;
        addOption(
          "step",
          t("react.workflows.display_step_variables", "Step variables"),
          `state.${name}.${memberName}`,
          { ...member, step_id: stepId },
          stepId ? t("react.workflows.display_state_alias_step", "state alias · from {step}", { step: stepId }) : "",
          "state_alias",
          `${textOrEmpty(context.producerLabels?.[stepId]) || humanize(stepId)} → ${textOrEmpty(member.label) || humanize(memberName)}`,
        );
      }
    }
    if (stepId) {
      addOption(
        `step:${stepId}`,
        t("react.workflows.display_step_group", "Step: {step}", { step: stepId }),
        `message.step.${stepId}.artifacts.${name}`,
        item,
        t("react.workflows.display_explicit_step_artifact", "explicit step artifact"),
        "step_artifact",
        `${textOrEmpty(context.producerLabels?.[stepId]) || humanize(stepId)} → ${textOrEmpty(item.label) || humanize(name)}`,
      );
    }
  });

  return groups
    .map((group) => ({
      ...group,
      options: [...group.options].sort((left, right) =>
        (left.label || left.value).localeCompare(right.label || right.value),
      ),
    }))
    .filter((group) => group.options.length);
}

export function workflowVariableSuggestionPanelId(key: string): string {
  return `workflow-variable-suggestions-${domIdSegment(key)}`;
}

export const BUILDER_DRAWER_WIDTH_STORAGE_KEY =
  "workflow-builder:drawer-width:v1";
export const BUILDER_DRAWER_DEFAULT_WIDTH = 450;
export const BUILDER_DRAWER_MIN_WIDTH = 420;
export const BUILDER_DRAWER_MAX_WIDTH = 720;
export const BUILDER_DRAWER_RESIZE_BREAKPOINT = 900;
export const WORKFLOW_CONTRACT_SECTION_STORAGE_KEY =
  "workflow-builder:workflow-contract-sections:v1";

export type WorkflowContractSectionKey = "inputs" | "outputs";

export function readWorkflowContractSectionState(): Partial<
  Record<WorkflowContractSectionKey, boolean>
> {
  if (globalThis.window === undefined) {
    return {};
  }
  try {
    const raw = globalThis.localStorage.getItem(
      WORKFLOW_CONTRACT_SECTION_STORAGE_KEY,
    );
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return parsed as Partial<Record<WorkflowContractSectionKey, boolean>>;
  } catch {
    return {};
  }
}

export function persistWorkflowContractSectionState(
  state: Partial<Record<WorkflowContractSectionKey, boolean>>,
) {
  if (globalThis.window === undefined) {
    return;
  }
  globalThis.localStorage.setItem(
    WORKFLOW_CONTRACT_SECTION_STORAGE_KEY,
    JSON.stringify(state),
  );
}

export function workflowContractIssueSection(
  issue: WorkflowCanvasIssue | null,
): WorkflowContractSectionKey | null {
  const path = textOrEmpty(issue?.field_path).toLowerCase();
  if (!path) {
    return null;
  }
  if (path.includes("expected_inputs") || path.includes("input_schema")) {
    return "inputs";
  }
  // Returned files are outputs; a setup issue about one opens Outputs.
  if (
    path.includes("expected_outputs") ||
    path.includes("returned_files") ||
    path.includes("expected_artifacts")
  ) {
    return "outputs";
  }
  return null;
}

export function clampBuilderDrawerWidth(
  width: number,
  viewportWidth?: number,
): number {
  const viewportMax =
    viewportWidth && viewportWidth > 0
      ? Math.max(BUILDER_DRAWER_MIN_WIDTH, viewportWidth - 32)
      : BUILDER_DRAWER_MAX_WIDTH;
  const safeMax = Math.min(BUILDER_DRAWER_MAX_WIDTH, viewportMax);
  return Math.min(
    safeMax,
    Math.max(BUILDER_DRAWER_MIN_WIDTH, Math.round(width)),
  );
}

export function readStoredBuilderDrawerWidth(): number {
  if (globalThis.window === undefined) {
    return BUILDER_DRAWER_DEFAULT_WIDTH;
  }
  try {
    const stored = globalThis.localStorage.getItem(
      BUILDER_DRAWER_WIDTH_STORAGE_KEY,
    );
    const parsed = stored ? Number.parseInt(stored, 10) : Number.NaN;
    return Number.isFinite(parsed)
      ? clampBuilderDrawerWidth(parsed, globalThis.innerWidth)
      : BUILDER_DRAWER_DEFAULT_WIDTH;
  } catch {
    return BUILDER_DRAWER_DEFAULT_WIDTH;
  }
}

export function persistBuilderDrawerWidth(width: number) {
  if (globalThis.window === undefined) {
    return;
  }
  try {
    globalThis.localStorage.setItem(
      BUILDER_DRAWER_WIDTH_STORAGE_KEY,
      stringFromUnknownValue(
        clampBuilderDrawerWidth(width, globalThis.innerWidth),
      ),
    );
  } catch {
    // Ignore storage failures; resizing should still work for this session.
  }
}

export type WorkflowValidationIssueGroup = {
  affectedLabels: string[];
  category: string;
  code: string;
  inputName: string;
  issues: Array<Record<string, unknown>>;
  key: string;
  messages: string[];
  severity: "error" | "warning";
  source: string;
  sourceName: string;
  sourceStepId: string;
  targetGroup: WorkflowGroupRow | null;
  targetStep: WorkflowStepRow | null;
  title: string;
};

declare global {
  interface Window {
    WF_ID?: string;
    DRAWER_STATE?: {
      mode: string;
      tab: string;
    };
    STEP_MODAL_STATE?: {
      editing: boolean;
    };
    PUBLISHED_BLOCKS?: Array<Record<string, unknown>>;
    _advancedMode?: boolean;
    AUTOMATION_SCOPE?: {
      kind: string;
    };
    _hasResolvablePublishedBlocks?: () => boolean;
    addStep?: (groupId?: string, laneId?: string) => void;
    closeBuilderDrawer?: () => void;
    toggleAdvancedMode?: () => void;
    openGroupDrawer?: (
      groupId: string,
      options?: { defaultTab?: string },
    ) => void;
    editStep?: (stepId: string) => void;
    openLibraryDrawerForStepRecovery?: () => void;
    addStepFromLibrary?: (blockKey: string) => Promise<void>;
    _publishedBlockDisplayName?: (blockKey: string) => string;
    switchBuilderDrawerTab?: (tab: string) => void;
    _setStepExecutorControlState?: (input: {
      skillId?: string;
      mappedAgents?: string[];
      resolvedAgent?: string;
    }) => void;
    addReaction?: () => void;
    switchTab?: (name: string) => void;
  }
}
