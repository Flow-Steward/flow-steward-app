import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useState, type ReactNode } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router";

import {
  DsAlert,
  DsBadge,
  DsCard,
  DsCheckbox,
  DsCheckboxMark,
  DsPageHeader,
  DsSelect,
  DsTable,
  type DsTableColumn,
} from "@/components/shared/design-system";
import {
  fetchSecurityPolicyRecord,
  saveSecurityPolicyRecord,
  type SettingsEntity,
} from "@/pages/settings/shared/api";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

import "@/pages/settings/shared/settings.css";

type Primitive = boolean | number | string;

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function objectOrEmpty(value: unknown): SettingsEntity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as SettingsEntity) : {};
}

function deepMergeSettings(base: SettingsEntity, patch: SettingsEntity): SettingsEntity {
  const merged: SettingsEntity = { ...base };
  Object.entries(patch).forEach(([key, value]) => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      merged[key] = deepMergeSettings(objectOrEmpty(base[key]), value as SettingsEntity);
      return;
    }
    merged[key] = value;
  });
  return merged;
}

function withNestedValue(base: SettingsEntity, path: readonly string[], value: unknown): SettingsEntity {
  const [head, ...tail] = path;
  if (!head) {
    return base;
  }
  if (!tail.length) {
    return { ...base, [head]: value };
  }
  return {
    ...base,
    [head]: withNestedValue(objectOrEmpty(base[head]), tail, value),
  };
}

function withoutTopLevelKeys(base: SettingsEntity, keys: readonly string[]): SettingsEntity {
  const omitted = new Set(keys);
  return Object.fromEntries(Object.entries(base).filter(([key]) => !omitted.has(key)));
}

function listOfStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => stringFromUnknownValue(item).trim()).filter(Boolean)
    : [];
}

function toggleListValue(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function isPrimitive(value: unknown): value is Primitive {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

const POLICY_LEVEL_OPTIONS = ["minimal", "standard", "strict"] as const;
const SEVERITY_OPTIONS = ["low", "medium", "high", "critical"] as const;
const SURFACE_ACTION_OPTIONS = ["scan", "skip"] as const;
const PII_MODE_OPTIONS = ["pseudonymize", "hard_redact", "allow"] as const;
const INJECTION_CONTEXT_OPTIONS = ["prompt", "display", "shell"] as const;
const SCANNER_KEYS = ["secrets", "injection", "pii"] as const;
const SURFACE_KEYS = ["inbound", "outbound", "tool"] as const;
const CUSTOM_RULE_SCANNERS = ["secrets", "injection", "pii"] as const;



function getPolicyFieldLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  enabled: t("monitoring.flags.enabled", "Enabled"),
  level: t("react.settings.level", "Level"),
};
}



function getPostureFieldLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  category_scores: t("react.settings.category_scores", "Category scores"),
  finding_counts: t("react.settings.findings", "Findings"),
  level: t("react.settings.level", "Level"),
  score: t("react.settings.score", "Score"),
  snapshot_at: t("react.settings.snapshot", "Snapshot"),
  status: t("account_settings.card.status", "Status"),
  scan_mode: t("security_overview.scan_mode", "Scan Mode"),
  enforcement_blocking: t("react.settings.security_enforcement_blocking", "Enforcement Blocking"),
};
}



function getPolicyLevelDescriptions(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  minimal: t("react.settings.lighter_scanning_pii_scanning_is_disabled_and_thresholds_are_higher", "Lighter scanning. PII scanning is disabled and thresholds are higher."),
  standard: t("react.settings.default_scanner_coverage_for_secrets_prompt_injection_pii_and_protected_runtime_surfaces", "Default scanner coverage for secrets, prompt injection, PII, and protected runtime surfaces."),
  strict: t("react.settings.most_sensitive_preset_lower_thresholds_and_hard_pii_redaction", "Most sensitive preset. Lower thresholds and hard PII redaction."),
};
}



function getScannerCopy(t: I18nContextValue["t"] = fallbackContext.t): Record<string, { title: string; description: string }> {
  return {
  secrets: {
    title: t("project_edit.options.secrets", "Secrets"),
    description: t("react.settings.detects_api_keys_tokens_private_keys_and_connection_strings", "Detects API keys, tokens, private keys, and connection strings."),
  },
  injection: {
    title: t("react.settings.prompt_injection", "Prompt injection"),
    description: t("react.settings.detects_instruction_hijacking_exfiltration_ssrf_and_shell_command_patterns", "Detects instruction hijacking, exfiltration, SSRF, and shell-command patterns."),
  },
  pii: {
    title: t("project_edit.options.pii", "PII"),
    description: t("react.settings.detects_names_email_phone_numbers_ssns_payment_cards_and_ip_addresses", "Detects names, email, phone numbers, SSNs, payment cards, and IP addresses."),
  },
};
}



function getSurfaceCopy(t: I18nContextValue["t"] = fallbackContext.t): Record<string, { title: string; description: string }> {
  return {
  inbound: {
    title: t("react.settings.inbound_input", "Inbound input"),
    description: t("react.settings.user_and_external_input_before_it_enters_an_agent_or_workflow", "User and external input before it enters an agent or workflow."),
  },
  outbound: {
    title: t("react.settings.outbound_output", "Outbound output"),
    description: t("react.settings.model_workflow_and_agent_output_before_it_leaves_the_system", "Model, workflow, and agent output before it leaves the system."),
  },
  tool: {
    title: t("react.settings.tool_activity", "Tool activity"),
    description: t("react.settings.tool_calls_tool_results_and_runtime_actions", "Tool calls, tool results, and runtime actions."),
  },
};
}

function humanizeKey(key: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const optionLabels: Record<string, string> = {
    minimal: t("react.settings.security_option_minimal", "Minimal"),
    standard: t("react.settings.security_option_standard", "Standard"),
    strict: t("react.settings.security_option_strict", "Strict"),
    low: t("react.settings.security_option_low", "Low"),
    medium: t("react.settings.security_option_medium", "Medium"),
    high: t("react.settings.security_option_high", "High"),
    critical: t("react.settings.security_option_critical", "Critical"),
    pseudonymize: t("react.settings.security_option_pseudonymize", "Pseudonymize"),
    redact: t("react.settings.security_option_redact", "Redact"),
    pass: t("react.settings.security_option_pass", "Pass"),
    block: t("react.settings.security_option_block", "Block"),
    allow: t("react.settings.security_option_allow", "Allow"),
    warn: t("react.settings.security_option_warn", "Warn"),
    email: t("react.settings.security_option_email", "Email"),
    phone: t("react.settings.security_option_phone", "Phone"),
    person: t("react.settings.security_option_person", "Person"),
    ip_address: t("react.settings.security_option_ip_address", "Ip Address"),
    payment_card: t("react.settings.security_option_payment_card", "Payment Card"),
    secrets: t("react.settings.security_option_secrets", "Secrets"),
    injection: t("react.settings.security_option_injection", "Injection"),
    input: t("react.settings.security_option_input", "Input"),
    output: t("react.settings.security_option_output", "Output"),
    tool: t("react.settings.security_option_tool", "Tool"),
    scan: t("react.settings.security_option_scan", "Scan"),
    skip: t("react.settings.security_option_skip", "Skip"),
    hard_redact: t("react.settings.security_option_hard_redact", "Hard Redact"),
    prompt: t("react.settings.security_option_prompt", "Prompt"),
    display: t("react.settings.security_option_display", "Display"),
    shell: t("react.settings.security_option_shell", "Shell"),
    person_name: t("react.settings.security_option_person_name", "Person Name"),
    credit_card: t("react.settings.security_option_credit_card", "Credit Card"),
  };
  const translated = getPolicyFieldLabels(t)[key] ?? getPostureFieldLabels(t)[key] ?? optionLabels[key];
  if (translated) return translated;
  return key
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function postureDisplayValue(key: string, value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (key === "score" && typeof value === "number") {
    return `${Math.round(value)}%`;
  }
  const raw = stringFromUnknownValue(value ?? "").trim();
  if (key === "level") {
    const levels: Record<string, string> = {
      hardened: t("security_overview.level.hardened", "Hardened"),
      secure: t("security_overview.level.secure", "Secure"),
      needs_attention: t("security_overview.level.needs_attention", "Needs attention"),
      at_risk: t("security_overview.level.at_risk", "At risk"),
      unknown: t("security_overview.level.unknown", "Unknown"),
    };
    if (levels[raw]) return levels[raw];
  }
  if (key === "scan_mode") {
    const modes: Record<string, string> = {
      enforce_standard: t("react.settings.security_mode_enforce_standard", "Enforce Standard"),
      enforce_strict: t("react.settings.security_mode_enforce_strict", "Enforce Strict"),
      monitor: t("react.settings.security_mode_monitor", "Monitor"),
      disabled: t("react.settings.security_mode_disabled", "Disabled"),
    };
    if (modes[raw]) return modes[raw];
  }
  if (key === "enforcement_blocking" && typeof value === "boolean") {
    return value ? t("react.workflows.true", "True") : t("react.workflows.false", "False");
  }
  if (key === "status" && raw === "no_data") {
    return t("react.settings.waiting_for_first_scan", "Waiting for first scan");
  }
  if (!raw) {
    return "—";
  }
  return raw.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function postureEntries(
  payload: SettingsEntity,
  { projectSecurity }: Readonly<{ projectSecurity: boolean }>,
): Array<[string, unknown]> {
  if (projectSecurity) {
    return Object.entries(payload);
  }
  const preferred = ["score", "level", "status", "snapshot_at", "finding_counts", "category_scores"];
  const seen = new Set<string>();
  const rows: Array<[string, unknown]> = [];
  preferred.forEach((key) => {
    if (key in payload) {
      seen.add(key);
      rows.push([key, payload[key]]);
    }
  });
  Object.entries(payload).forEach(([key, value]) => {
    if (!seen.has(key)) {
      rows.push([key, value]);
    }
  });
  return rows;
}

function scannerRows(policy: SettingsEntity, t: I18nContextValue["t"] = fallbackContext.t) {
  const scanners = objectOrEmpty(policy.scanners);
  if (!Object.keys(scanners).length) {
    return [];
  }
  return SCANNER_KEYS
    .filter((key) => key in scanners)
    .map((key) => {
      const config = objectOrEmpty(scanners[key]);
      const enabled = config.enabled !== false;
      const threshold = stringFromUnknownValue(config.severity_threshold || "low");
      const meta = [
        enabled ? t("react.settings.enabled", "Enabled") : t("react.settings.disabled", "Disabled"),
        t("react.settings.scanner_threshold_detail", "Threshold: {value}", { value: humanizeKey(threshold, t) }),
      ];
      const allowlist = listOfStrings(config.allowlist);
      const contexts = listOfStrings(config.contexts);
      const mode = stringFromUnknownValue(config.mode || "pseudonymize");
      const detectors = objectOrEmpty(config.detectors);
      const detectorRows = Object.entries(detectors).map(([detector, detectorConfig]) => ({
        key: detector,
        title: humanizeKey(detector, t),
        enabled: objectOrEmpty(detectorConfig).enabled !== false,
      }));
      if (key === "injection") {
        if (contexts.length) {
          meta.push(t("react.settings.scanner_contexts_detail", "Contexts: {value}", { value: contexts.map((context) => humanizeKey(context, t)).join(", ") }));
        }
      }
      if (key === "pii") {
        meta.push(t("react.settings.scanner_mode_detail", "Mode: {value}", { value: humanizeKey(mode, t) }));
        const enabledDetectors = detectorRows.filter((detector) => detector.enabled).map((detector) => detector.title);
        if (enabledDetectors.length) {
          meta.push(t("react.settings.scanner_detectors_detail", "Detectors: {value}", { value: enabledDetectors.join(", ") }));
        }
      }
      if (allowlist.length) {
        meta.push(t("react.settings.scanner_allowlist_count", "Allowlist: {count}", { count: allowlist.length }));
      }
      const copy = getScannerCopy(t)[key] || { title: humanizeKey(key, t), description: t("react.settings.custom_scanner_configuration", "Custom scanner configuration.") };
      return { key, ...copy, contexts, detectorRows, enabled, meta, mode, threshold };
    });
}

function surfaceRows(policy: SettingsEntity, t: I18nContextValue["t"] = fallbackContext.t) {
  const surfaces = objectOrEmpty(policy.surfaces);
  if (!Object.keys(surfaces).length) {
    return [];
  }
  return SURFACE_KEYS
    .filter((key) => key in surfaces)
    .map((key) => {
      const config = objectOrEmpty(surfaces[key]);
      const action = stringFromUnknownValue(config.action || "scan").trim() || "scan";
      const copy = getSurfaceCopy(t)[key] || { title: humanizeKey(key, t), description: t("react.settings.custom_runtime_surface", "Custom runtime surface.") };
      return { key, ...copy, action };
    });
}

function customRulePayloads(policy: SettingsEntity): SettingsEntity[] {
  const customRules = Array.isArray(policy.custom_rules) ? policy.custom_rules : [];
  return customRules.filter(
    (rule): rule is SettingsEntity => Boolean(rule) && typeof rule === "object" && !Array.isArray(rule),
  );
}

function customRuleRows(policy: SettingsEntity, t: I18nContextValue["t"] = fallbackContext.t) {
  return customRulePayloads(policy)
    .map((rule, index) => {
      const ruleId = stringFromUnknownValue(rule.rule_id || `Rule ${index + 1}`);
      const scanner = stringFromUnknownValue(rule.scanner || "");
      const severity = stringFromUnknownValue(rule.severity || "");
      const description = stringFromUnknownValue(rule.description || t("react.settings.custom_scanner_rule", "Custom scanner rule."));
      return {
        key: `${ruleId}-${index}`,
        title: ruleId,
        description,
        index,
        meta: [scanner ? t("react.settings.rule_scanner_detail", "Scanner: {value}", { value: humanizeKey(scanner, t) }) : "", severity ? t("react.settings.rule_severity_detail", "Severity: {value}", { value: humanizeKey(severity, t) }) : ""]
          .filter(Boolean),
      };
    });
}

type CustomRuleRow = ReturnType<typeof customRuleRows>[number];

function SecurityDetailRow({ label, value }: Readonly<{ label: string; value: ReactNode }>) {
  return (
    <div className="fs-detail-row">
      <span>{label}</span>
      <span className="fs-detail-value">{value}</span>
    </div>
  );
}

/**
 * The published checkbox anatomy with an accessible name of its own. The visible
 * text is a state ("On"/"Off") or a short option, not the control's name.
 */
function LabelledCheckbox({
  ariaLabel,
  checked,
  onChange,
  children,
}: Readonly<{ ariaLabel: string; checked: boolean; onChange: (checked: boolean) => void; children: ReactNode }>) {
  return (
    <label className="fs-checkbox-label">
      <input aria-label={ariaLabel} checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
      <DsCheckboxMark />
      <span>{children}</span>
    </label>
  );
}

function customRuleColumns(
  onRemove: (index: number) => void,
  t: I18nContextValue["t"],
): Array<DsTableColumn<CustomRuleRow>> {
  return [
    {
      key: "rule",
      header: t("registry.guardrails.column.rule", "Rule"),
      cell: (row: CustomRuleRow) => (
        <span className="fs-user-copy">
          <strong>{row.title}</strong>
          <span>{row.description}</span>
        </span>
      ),
    },
    { key: "meta", header: t("react.settings.scanner_severity", "Scanner · severity"), cell: (row: CustomRuleRow) => row.meta.join(" · ") || null },
    {
      key: "actions",
      header: "",
      ariaLabel: t("monitoring.table.actions", "Actions"),
      cell: (row: CustomRuleRow) => (
        <button className="fs-button ghost-danger" onClick={() => onRemove(row.index)} type="button">
          {t("registry.agent_detail.common.remove", "Remove")} </button>
      ),
    },
  ];
}

export function SettingsSecurityPage({
  embedded = false,
}: Readonly<{
  /** Inside the Settings hub, which owns the page header. */
  embedded?: boolean;
}> = {}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { projectId: rawProjectId } = useParams();
  const projectId = firstStringFromUnknownValue([rawProjectId, ""]).trim();
  const isProjectSecurity = Boolean(projectId);
  const [policyEdits, setPolicyEdits] = useState<SettingsEntity>({});
  const [clearedOverrideKeys, setClearedOverrideKeys] = useState<string[]>([]);
  const [policyMessage, setPolicyMessage] = useState("");
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [customRuleDraft, setCustomRuleDraft] = useState<SettingsEntity>({
    description: "",
    pattern: "",
    rule_id: "",
    scanner: "secrets",
    severity: "medium",
  });

  const policyQuery = useQuery({
    queryKey: ["settings", "security", "policy", projectId || "global"],
    queryFn: () => fetchSecurityPolicyRecord(projectId || undefined),
  });

  const policyPayload = objectOrEmpty(policyQuery.data?.policy);
  const posturePayload = objectOrEmpty(policyQuery.data?.posture);
  const projectOverridePayload = objectOrEmpty(policyQuery.data?.project_override);
  const globalPolicyPayload = objectOrEmpty(policyQuery.data?.global_policy);
  const inheritedPolicyPayload = clearedOverrideKeys.reduce(
    (current, key) => key in globalPolicyPayload ? { ...current, [key]: globalPolicyPayload[key] } : current,
    policyPayload,
  );
  const editablePolicyPayload = deepMergeSettings(inheritedPolicyPayload, policyEdits);
  const primitivePolicyRows = Object.entries(editablePolicyPayload).filter(([, value]) => isPrimitive(value));
  const complexPolicyRows = Object.entries(editablePolicyPayload).filter(([, value]) => !isPrimitive(value));
  const postureStatus = stringFromUnknownValue(posturePayload.status || "").trim();
  // AC17-R74: a posture score describes findings; it says nothing about whether a finding
  // would be acted on. A production install running in monitor mode scanned everything,
  // blocked nothing, and reported "Score 100%, Hardened" — the runtime says so at every
  // start, the screen never did. `enforcement_blocking` is absent on older runtimes, so the
  // notice only appears when the payload explicitly says blocking is off.
  const scanMode = stringFromUnknownValue(posturePayload.scan_mode || "").trim();
  const enforcementBlocking = posturePayload.enforcement_blocking;
  const enforcementDisabled = enforcementBlocking === false;
  const postureUnavailable = !isProjectSecurity && ["no_data", "error"].includes(postureStatus);
  const postureRows = postureUnavailable ? [] : postureEntries(posturePayload, { projectSecurity: isProjectSecurity });
  const scannerPolicyRows = scannerRows(editablePolicyPayload, t);
  const surfacePolicyRows = surfaceRows(editablePolicyPayload, t);
  const customPolicyRows = customRuleRows(editablePolicyPayload, t);
  const customPolicyPayloads = customRulePayloads(editablePolicyPayload);
  const additionalComplexPolicyRows = complexPolicyRows.filter(
    ([key]) => !["scanners", "surfaces", "custom_rules"].includes(key),
  );
  const policyLevel = stringFromUnknownValue(editablePolicyPayload.level ?? "").trim();
  const pageTitle = isProjectSecurity ? t("react.settings.project_security", "Project security") : t("account.security.title", "Security");
  const pageDescription = isProjectSecurity
    ? t("react.settings.security_policy_override_for_project_value1", "Security policy override for project {value1}.", { value1: String(projectId) })
    : t("react.settings.posture_is_a_read_only_findings_rollup_policy_controls_which_security_scanners_run", "Posture is a read-only findings rollup; policy controls which security scanners run.");
  const postureTitle = isProjectSecurity ? t("react.settings.policy_sources", "Policy sources") : t("react.settings.security_posture", "Security posture");
  const postureDescription = isProjectSecurity
    ? t("react.settings.shows_whether_each_effective_policy_value_comes_from_the_global_policy_or_the_project_override", "Shows whether each effective policy value comes from the global policy or the project override.")
    : t("react.settings.read_only_score_from_security_findings_logged_during_scanner_events_background_control_plane_jobs_refresh_snap", "Read-only score from security findings logged during scanner events. Background control-plane jobs refresh snapshots automatically.");
  const policyDescription = isProjectSecurity
    ? t("react.settings.project_overrides_only_replace_fields_listed_here_every_other_field_is_inherited_from_the_account_policy", "Project overrides only replace fields listed here; every other field is inherited from the account policy.")
    : t("react.settings.account_wide_defaults_projects_inherit_these_settings_first_then_project_overrides_can_replace_individual_fiel", "Account-wide defaults. Projects inherit these settings first, then project overrides can replace individual fields.");
  const pendingOverrideKeys = [...new Set([
    ...Object.keys(policyEdits),
    ...clearedOverrideKeys,
  ])].sort((left, right) => left.localeCompare(right));
  const pendingOverrideSummary = pendingOverrideKeys.length
    ? t("react.settings.pending_override_changes_value1", "Pending override changes: {value1}", { value1: String(pendingOverrideKeys.join(", ")) })
    : t("react.settings.no_pending_override_changes", "No pending override changes.");

  function updatePolicyEdit(path: readonly string[], value: unknown) {
    if (path[0]) {
      setClearedOverrideKeys((current) => current.filter((key) => key !== path[0]));
    }
    setPolicyEdits((current) => withNestedValue(current, path, value));
  }

  function clearProjectOverride(key: string) {
    setPolicyEdits((current) => withoutTopLevelKeys(current, [key]));
    setClearedOverrideKeys((current) => current.includes(key) ? current : [...current, key]);
  }

  function handleAddCustomRule() {
    const ruleId = stringFromUnknownValue(customRuleDraft.rule_id || "").trim();
    const pattern = stringFromUnknownValue(customRuleDraft.pattern || "").trim();
    if (!ruleId || !pattern) {
      setPolicyMessage(t("react.settings.custom_rule_needs_a_rule_id_and_a_pattern_before_it_can_be_added", "Custom rule needs a rule id and a pattern before it can be added."));
      return;
    }
    const rule: SettingsEntity = {
      description: stringFromUnknownValue(customRuleDraft.description || "").trim(),
      pattern,
      rule_id: ruleId,
      scanner: stringFromUnknownValue(customRuleDraft.scanner || "secrets").trim() || "secrets",
      severity: stringFromUnknownValue(customRuleDraft.severity || "medium").trim() || "medium",
    };
    updatePolicyEdit(["custom_rules"], [...customPolicyPayloads, rule]);
    setCustomRuleDraft({
      description: "",
      pattern: "",
      rule_id: "",
      scanner: "secrets",
      severity: "medium",
    });
    setPolicyMessage(t("react.settings.custom_rule_added_save_policy_to_persist_it", "Custom rule added. Save policy to persist it."));
  }

  function handleRemoveCustomRule(index: number) {
    updatePolicyEdit(
      ["custom_rules"],
      customPolicyPayloads.filter((_, ruleIndex) => ruleIndex !== index),
    );
    setPolicyMessage(t("react.settings.custom_rule_removed_save_policy_to_persist_the_change", "Custom rule removed. Save policy to persist the change."));
  }

  async function handleSavePolicy() {
    setPolicyMessage("");
    setSavingPolicy(true);
    try {
      const payloadBase = isProjectSecurity
        ? withoutTopLevelKeys(projectOverridePayload, clearedOverrideKeys)
        : policyPayload;
      const payload = deepMergeSettings(payloadBase, policyEdits);
      await saveSecurityPolicyRecord(payload, projectId || undefined);
      setPolicyEdits({});
      setClearedOverrideKeys([]);
      setPolicyMessage(isProjectSecurity ? t("react.settings.project_security_policy_updated", "Project security policy updated.") : t("react.settings.security_policy_updated", "Security policy updated."));
      await queryClient.invalidateQueries({
        queryKey: ["settings", "security", "policy", projectId || "global"],
      });
    } catch (error) {
      setPolicyMessage(error instanceof Error ? error.message : t("react.settings.failed_to_save_security_policy", "Failed to save security policy."));
    } finally {
      setSavingPolicy(false);
    }
  }

  const overrideActive = (key: string) =>
    isProjectSecurity && key in projectOverridePayload && !clearedOverrideKeys.includes(key);

  function inheritButton(key: string) {
    return overrideActive(key) ? (
      <button
        aria-label={t("react.settings.inherit_value1", "Inherit {value1}", { value1: String(humanizeKey(key, t)) })}
        className="fs-button ghost"
        onClick={() => clearProjectOverride(key)}
        type="button"
      >
        {t("react.settings.inherit", "Inherit")} </button>
    ) : null;
  }

  const content = (
    <>
      <DsCard description={postureDescription} title={postureTitle}>
        {renderWhen(enforcementDisabled, (
          <div data-testid="security-enforcement-notice">
            <DsAlert
              title={scanMode === "disabled" ? t("react.settings.scanning_is_turned_off", "Scanning is turned off.") : t("react.settings.scanning_is_active_but_nothing_is_being_blocked", "Scanning is active, but nothing is being blocked.")}
              tone="warning"
            >
              {scanMode === "disabled"
                ? t("react.settings.the_scanners_below_describe_a_policy_that_is_not_being_applied", "The scanners below describe a policy that is not being applied.")
                : t("react.settings.scan_mode_is_value1_findings_are_recorded_and_redaction_still_applies_to_stored_data_but_an_operation_with_a_f", "Scan mode is {value1}: findings are recorded and redaction still applies to stored data, but an operation with a finding is not stopped. The scores and scanner settings below describe the policy, not what is enforced.", { value1: String(scanMode || "monitor") })}
            </DsAlert>
          </div>
        ))}
        {renderWhen(postureUnavailable, (
          <DsAlert title={postureStatus === "error" ? t("react.settings.posture_snapshot_unavailable", "Posture snapshot unavailable.") : t("react.settings.no_posture_snapshot_yet", "No posture snapshot yet.")}>
            {t("react.settings.this_page_does_not_start_a_separate_scan_the_posture_job_periodically_scores_the_last_24_hours_of_recorded_sec", "This page does not start a separate scan. The posture job periodically scores the last 24 hours of recorded security findings for each active project; the first snapshot appears after that background job runs.")} </DsAlert>
        ))}
        {renderWhen(!postureUnavailable && Boolean(postureRows.length), (
          <div className="fs-detail-rows" data-columns="2">
            {postureRows.map(([key, value]) => (
              <SecurityDetailRow key={key} label={humanizeKey(key, t)} value={postureDisplayValue(key, value, t)} />
            ))}
          </div>
        ))}
        {renderWhen(!postureUnavailable && !postureRows.length, (
          <p className="fs-empty-copy">
            {isProjectSecurity ? t("react.settings.no_source_map_values_are_available", "No source map values are available.") : t("react.settings.waiting_for_first_posture_scan", "Waiting for first posture scan.")}
          </p>
        ))}
      </DsCard>

      <DsCard
        description={policyDescription}
        footer={
          <div className="fs-footer-actions">
            <button
              className="fs-button primary"
              disabled={savingPolicy}
              onClick={() => ignoreAsyncResult(handleSavePolicy())}
              type="button"
            >
              {savingPolicy ? t("project_edit.security.saving", "Saving...") : t("settings.retention.save_policy", "Save policy")}
            </button>
          </div>
        }
        title={t("react.settings.security_policy", "Security policy")}
      >
        {renderWhen(policyQuery.isError, (
          <DsAlert title={t("react.settings.security_policy_could_not_be_loaded", "Security policy could not be loaded")} tone="danger">
            {policyQuery.error instanceof Error ? policyQuery.error.message : t("react.settings.failed_to_load_security_policy", "Failed to load security policy.")}
          </DsAlert>
        ))}

        {renderWhen(isProjectSecurity, (
          <div className="fs-panel-heading">
            <p className="fs-ink-secondary">{pendingOverrideSummary}</p>
            {Object.keys(projectOverridePayload).length ? (
              <button
                className="fs-button"
                onClick={() => {
                  setPolicyEdits({});
                  setClearedOverrideKeys(Object.keys(projectOverridePayload));
                }}
                type="button"
              >
                {t("react.settings.clear_all_project_overrides", "Clear all project overrides")} </button>
            ) : null}
          </div>
        ))}

        {renderWhen(Boolean(primitivePolicyRows.length), (
          <div className="fs-form-grid">
            {primitivePolicyRows.map(([key, originalValue]) => {
              const value = originalValue as Primitive;
              if (typeof originalValue === "boolean") {
                return (
                  <div className="fs-panel-heading" key={key}>
                    <DsCheckbox checked={Boolean(value)} name={key} onChange={(checked) => updatePolicyEdit([key], checked)}>
                      {humanizeKey(key, t)}
                    </DsCheckbox>
                    {inheritButton(key)}
                  </div>
                );
              }
              if (key === "level") {
                return (
                  <div className="fs-field fs-settings-span" key={key}>
                    <div className="fs-panel-heading">
                      <label className="fs-field-label" htmlFor="security-policy-level">{humanizeKey(key, t)}</label>
                      {inheritButton(key)}
                    </div>
                    <DsSelect
                      ariaLabel={humanizeKey(key, t)}
                      id="security-policy-level"
                      onChange={(next) => updatePolicyEdit([key], next)}
                      options={POLICY_LEVEL_OPTIONS.map((option) => ({ value: option, label: humanizeKey(option, t) }))}
                      value={stringFromUnknownValue(value)}
                    />
                    <span className="fs-field-helper">
                      {getPolicyLevelDescriptions(t)[policyLevel] || t("react.settings.preset_for_scanner_thresholds_and_redaction_behavior", "Preset for scanner thresholds and redaction behavior.")}
                    </span>
                  </div>
                );
              }
              return (
                <div className="fs-field" key={key}>
                  <div className="fs-panel-heading">
                    <label className="fs-field-label" htmlFor={`security-policy-${key}`}>{humanizeKey(key, t)}</label>
                    {inheritButton(key)}
                  </div>
                  <input
                    aria-label={humanizeKey(key, t)}
                    className="fs-input"
                    id={`security-policy-${key}`}
                    onChange={(event) =>
                      updatePolicyEdit(
                        [key],
                        typeof originalValue === "number" ? Number(event.target.value || 0) : event.target.value,
                      )
                    }
                    type={typeof originalValue === "number" ? "number" : "text"}
                    value={stringFromUnknownValue(value)}
                  />
                </div>
              );
            })}
          </div>
        ))}

        {renderWhen(!primitivePolicyRows.length, (
          <p className="fs-empty-copy">{t("react.settings.no_primitive_policy_fields_are_available_for_inline_editing", "No primitive policy fields are available for inline editing.")}</p>
        ))}

        {renderWhen(Boolean(complexPolicyRows.length), (
          <>
            {renderWhen(Boolean(scannerPolicyRows.length), (
              <div className="fs-panel-group">
                <h3 className="fs-panel-title">{t("react.settings.scanner_coverage", "Scanner coverage")}</h3>
                <div className="fs-form-grid">
                  {scannerPolicyRows.map((row) => (
                    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4" key={row.key}>
                      <div className="fs-panel-heading">
                        <div>
                          <h4 className="fs-panel-title">{row.title}</h4>
                          <p>{row.description}</p>
                        </div>
                        <LabelledCheckbox
                          ariaLabel={t("react.settings.value1_enabled", "{value1} enabled", { value1: String(row.title) })}
                          checked={row.enabled}
                          onChange={(checked) => updatePolicyEdit(["scanners", row.key, "enabled"], checked)}
                        >
                          {row.enabled ? t("react.settings.on", "On") : t("react.settings.off", "Off")}
                        </LabelledCheckbox>
                      </div>
                      <div className="fs-field">
                        <span className="fs-field-label">{t("react.settings.threshold", "Threshold")}</span>
                        <DsSelect
                          ariaLabel={t("react.settings.value1_threshold", "{value1} threshold", { value1: String(row.title) })}
                          onChange={(next) => updatePolicyEdit(["scanners", row.key, "severity_threshold"], next)}
                          options={SEVERITY_OPTIONS.map((option) => ({ value: option, label: humanizeKey(option, t) }))}
                          value={row.threshold}
                        />
                      </div>
                      {row.key === "injection" ? (
                        <fieldset className="fs-field">
                          <legend className="fs-field-label">{t("react.settings.contexts", "Contexts")}</legend>
                          <div className="fs-footer-actions">
                            {INJECTION_CONTEXT_OPTIONS.map((context) => (
                              <LabelledCheckbox
                                ariaLabel={t("react.settings.prompt_injection_value1_context", "Prompt injection {value1} context", { value1: String(context) })}
                                checked={row.contexts.includes(context)}
                                key={context}
                                onChange={() =>
                                  updatePolicyEdit(["scanners", row.key, "contexts"], toggleListValue(row.contexts, context))
                                }
                              >
                                {humanizeKey(context, t)}
                              </LabelledCheckbox>
                            ))}
                          </div>
                        </fieldset>
                      ) : null}
                      {row.key === "pii" ? (
                        <>
                          <div className="fs-field">
                            <span className="fs-field-label">{t("registry.guardrails.column.mode", "Mode")}</span>
                            <DsSelect
                              ariaLabel={t("react.settings.pii_mode", "PII mode")}
                              onChange={(next) => updatePolicyEdit(["scanners", row.key, "mode"], next)}
                              options={PII_MODE_OPTIONS.map((option) => ({ value: option, label: humanizeKey(option, t) }))}
                              value={row.mode}
                            />
                          </div>
                          {row.detectorRows.length ? (
                            <fieldset className="fs-field">
                              <legend className="fs-field-label">{t("react.settings.detectors", "Detectors")}</legend>
                              <div className="fs-footer-actions">
                                {row.detectorRows.map((detector) => (
                                  <LabelledCheckbox
                                    ariaLabel={t("react.settings.pii_value1_detector", "PII {value1} detector", { value1: String(detector.title) })}
                                    checked={detector.enabled}
                                    key={detector.key}
                                    onChange={(checked) =>
                                      updatePolicyEdit(["scanners", row.key, "detectors", detector.key, "enabled"], checked)
                                    }
                                  >
                                    {detector.title}
                                  </LabelledCheckbox>
                                ))}
                              </div>
                            </fieldset>
                          ) : null}
                        </>
                      ) : null}
                      <div className="fs-footer-actions">
                        {row.meta.map((item) => (
                          <DsBadge key={item}>{item}</DsBadge>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}

            {renderWhen(Boolean(surfacePolicyRows.length), (
              <div className="fs-panel-group">
                <h3 className="fs-panel-title">{t("react.settings.scan_surfaces", "Scan surfaces")}</h3>
                <div className="fs-form-grid">
                  {surfacePolicyRows.map((row) => (
                    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4" key={row.key}>
                      <div className="fs-panel-heading">
                        <div>
                          <h4 className="fs-panel-title">{row.title}</h4>
                          <p>{row.description}</p>
                        </div>
                      </div>
                      <DsSelect
                        ariaLabel={t("react.settings.value1_action", "{value1} action", { value1: String(row.title) })}
                        onChange={(next) => updatePolicyEdit(["surfaces", row.key, "action"], next)}
                        options={SURFACE_ACTION_OPTIONS.map((option) => ({ value: option, label: humanizeKey(option, t) }))}
                        value={row.action}
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}

            <div className="fs-panel-group">
              <h3 className="fs-panel-title">{t("react.settings.custom_rules", "Custom rules")}</h3>
              <article className="fs-card fs-table-card">
                <DsTable
                  ariaLabel={t("react.settings.custom_rules", "Custom rules")}
                  columns={customRuleColumns(handleRemoveCustomRule, t)}
                  rowKey={(row: CustomRuleRow) => row.key}
                  rows={customPolicyRows}
                  state={
                    customPolicyRows.length ? null : (
                      <div className="fs-table-state">
                        <strong>{t("react.settings.no_custom_scanner_rules_are_configured", "No custom scanner rules are configured.")}</strong>
                      </div>
                    )
                  }
                />
              </article>
              <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4">
                <div className="fs-form-grid">
                  <label className="fs-field">
                    <span className="fs-field-label">{t("react.settings.rule_id", "Rule id")}</span>
                    <input
                      aria-label={t("react.settings.custom_rule_id", "Custom rule id")}
                      className="fs-input"
                      onChange={(event) => setCustomRuleDraft((current) => ({ ...current, rule_id: event.target.value }))}
                      placeholder={t("react.settings.custom_blocked_pattern", "custom.blocked_pattern")}
                      value={stringFromUnknownValue(customRuleDraft.rule_id || "")}
                    />
                  </label>
                  <div className="fs-field">
                    <span className="fs-field-label">{t("security_overview.columns.scanner", "Scanner")}</span>
                    <DsSelect
                      ariaLabel={t("react.settings.custom_rule_scanner", "Custom rule scanner")}
                      onChange={(next) => setCustomRuleDraft((current) => ({ ...current, scanner: next }))}
                      options={CUSTOM_RULE_SCANNERS.map((scanner) => ({ value: scanner, label: humanizeKey(scanner, t) }))}
                      value={stringFromUnknownValue(customRuleDraft.scanner || "secrets")}
                    />
                  </div>
                  <div className="fs-field">
                    <span className="fs-field-label">{t("events.columns.severity", "Severity")}</span>
                    <DsSelect
                      ariaLabel={t("react.settings.custom_rule_severity", "Custom rule severity")}
                      onChange={(next) => setCustomRuleDraft((current) => ({ ...current, severity: next }))}
                      options={SEVERITY_OPTIONS.map((severity) => ({ value: severity, label: humanizeKey(severity, t) }))}
                      value={stringFromUnknownValue(customRuleDraft.severity || "medium")}
                    />
                  </div>
                  <label className="fs-field">
                    <span className="fs-field-label">{t("react.settings.pattern", "Pattern")}</span>
                    <input
                      aria-label={t("react.settings.custom_rule_pattern", "Custom rule pattern")}
                      className="fs-input"
                      onChange={(event) => setCustomRuleDraft((current) => ({ ...current, pattern: event.target.value }))}
                      placeholder={t("react.settings.regular_expression", "Regular expression")}
                      value={stringFromUnknownValue(customRuleDraft.pattern || "")}
                    />
                  </label>
                  <label className="fs-field fs-settings-span">
                    <span className="fs-field-label">{t("registry.agents.field.description", "Description")}</span>
                    <input
                      aria-label={t("react.settings.custom_rule_description", "Custom rule description")}
                      className="fs-input"
                      onChange={(event) => setCustomRuleDraft((current) => ({ ...current, description: event.target.value }))}
                      placeholder={t("react.settings.what_this_rule_detects", "What this rule detects")}
                      value={stringFromUnknownValue(customRuleDraft.description || "")}
                    />
                  </label>
                </div>
                <div className="fs-footer-actions">
                  <button className="fs-button" onClick={handleAddCustomRule} type="button">
                    {t("react.settings.add_custom_rule", "Add custom rule")} </button>
                </div>
              </div>
            </div>

            {renderWhen(Boolean(additionalComplexPolicyRows.length), (
              <div className="fs-panel-group">
                <h3 className="fs-panel-title">{t("react.settings.additional_policy_data", "Additional policy data")}</h3>
                <div className="fs-footer-actions">
                  {additionalComplexPolicyRows.map(([key, value]) => (
                    <DsBadge key={key}>
                      {humanizeKey(key, t)}: {Array.isArray(value) ? t("react.settings.value1_items", "{value1} items", { value1: String(value.length) }) : "configured"}
                    </DsBadge>
                  ))}
                </div>
              </div>
            ))}
          </>
        ))}

        {renderWhen(Boolean(policyMessage), <DsAlert title={policyMessage} />)}
      </DsCard>
    </>
  );

  if (embedded) {
    return content;
  }
  return (
    <section className="fs-settings">
      <DsPageHeader description={pageDescription} eyebrow={t("shell.nav.settings", "Settings")} title={pageTitle} />
      {content}
    </section>
  );
}
