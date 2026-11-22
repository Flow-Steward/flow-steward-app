import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { DsBadge, DsDialog, DsRadio, DsSelect, DsTabs } from "@/components/shared/design-system";
import { DsAccordion } from "@/components/shared/ds-accordion";
import { DsSwitch } from "@/components/shared/ds-switch";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import type { ConditionRule, FormFieldView } from "@/components/workflow-inputs/form-field";
import { useI18n } from "@/lib/i18n/useI18n";
import type { OperatorIssue } from "@/pages/operator/types";
import {
  conditionValue,
  emptyFieldConfig,
  fieldPath,
  ownFieldIssues,
  schemaProperties,
  type OperatorCompatibility,
  type OperatorFieldConfig,
  type OperatorFieldLayout,
  type OperatorFieldTab,
} from "./detail-operator-settings-model";
import { kindLabel, operatorLabel, widgetDescription, widgetLabel } from "./detail-operator-labels";

export function IssueLines({ issues }: Readonly<{ issues: readonly OperatorIssue[] }>) {
  if (!issues.length) return null;
  return (
    <ul className="space-y-1">
      {issues.map((issue) => (
        <li className={`fs-field-helper ${issue.severity === "warning" ? "" : "error"}`.trim()} key={`${issue.code}:${issue.path}`} role={issue.severity === "warning" ? undefined : "alert"}>
          {issue.message}
        </li>
      ))}
    </ul>
  );
}

function issuesAtKey(issues: readonly OperatorIssue[], prefix: string, key: string): OperatorIssue[] {
  const path = `${prefix}.${key}`;
  return issues.filter((issue) => issue.path === path || (issue.path ?? "").startsWith(`${path}.`) || (issue.path ?? "").startsWith(`${path}[`));
}

/** The widget dialog: only the widgets that fit the field's type in the contract. */
function WidgetDialog({
  field,
  current,
  onClose,
  onChoose,
}: Readonly<{ field: FormFieldView; current: string; onClose: () => void; onChoose: (widget: string) => void }>) {
  const { t } = useI18n();
  const [chosen, setChosen] = useState(current);
  const name = useId();
  const widgets = field.widgets ?? [];
  return (
    <DsDialog
      description={t("react.workflows.operator.widget_dialog_help", "Only the widgets that fit the {type} type of this field in the contract are listed.", { type: kindLabel(field.kind, t) })}
      eyebrow={field.label || field.name}
      footer={<>
        <Button onClick={onClose} type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
        <Button onClick={() => onChoose(chosen)} type="button">{t("react.workflows.operator.use_widget", "Use widget")}</Button>
      </>}
      onClose={onClose}
      open
      title={t("react.workflows.operator.choose_widget", "Choose widget")}
    >
      <div className="space-y-2" data-testid="operator-widget-options">
        {widgets.map((widget) => (
          <div key={widget}>
            <DsRadio checked={chosen === widget} name={name} onChange={() => setChosen(widget)} value={widget}>
              {widgetLabel(widget, t)}
              {widget === widgets[0] ? <span className="fs-ink-tertiary">{" "}{t("react.workflows.operator.widget_default", "(default)")}</span> : null}
            </DsRadio>
            <p className="fs-field-helper ml-7">{widgetDescription(widget, t)}</p>
          </div>
        ))}
      </div>
    </DsDialog>
  );
}

function ConditionValueControl({
  kind,
  options,
  optionLabels,
  rule,
  label,
  onChange,
}: Readonly<{ kind: string; options: string[]; optionLabels: Record<string, string>; rule: ConditionRule; label: string; onChange: (value: unknown) => void }>) {
  const { t } = useI18n();
  if (rule.operator === "==empty" || rule.operator === "!=empty") return null;
  const raw = rule.value === null || rule.value === undefined ? "" : String(rule.value);
  if (kind === "boolean") {
    return <DsSelect ariaLabel={label} onChange={(value) => onChange(conditionValue(kind, rule.operator, value))} options={[
      { value: "true", label: t("react.workflows.true", "True") },
      { value: "false", label: t("react.workflows.false", "False") },
    ]} value={rule.value === true ? "true" : rule.value === false ? "false" : ""} />;
  }
  if (kind === "select" || kind === "select_list") {
    return <DsSelect ariaLabel={label} onChange={(value) => onChange(value)} options={options.map((option) => ({ value: option, label: optionLabels[option] || option }))} value={raw} />;
  }
  return <input aria-label={label} className="fs-input w-full" inputMode={kind === "number" ? "decimal" : undefined} onChange={(event) => onChange(conditionValue(kind, rule.operator, event.target.value))} type={kind === "number" ? "number" : "text"} value={raw} />;
}

/** «Show this field if…»: rules joined by "and" inside a group, groups joined by "or". */
function ConditionalLogicEditor({
  field,
  config,
  siblings,
  compatibility,
  issues,
  onChange,
}: Readonly<{
  field: FormFieldView;
  config: OperatorFieldConfig;
  siblings: readonly FormFieldView[];
  compatibility: OperatorCompatibility;
  issues: readonly OperatorIssue[];
  onChange: (groups: ConditionRule[][]) => void;
}>) {
  const { t } = useI18n();
  const others = siblings.filter((sibling) => sibling.name !== field.name);
  const groups = config.conditional_logic;
  const operatorsFor = (name: string) => compatibility.condition_operators_by_kind?.[others.find((other) => other.name === name)?.kind ?? ""] ?? [];
  const newRule = (): ConditionRule => {
    const first = others[0];
    const operator = first ? operatorsFor(first.name)[0] ?? "==" : "==";
    return { field: first?.name ?? "", operator, value: operator.endsWith("empty") ? null : first?.kind === "boolean" ? true : "" };
  };
  const updateRule = (groupIndex: number, ruleIndex: number, patch: Partial<ConditionRule>) => {
    onChange(groups.map((group, gIndex) => gIndex !== groupIndex ? group : group.map((rule, rIndex) => rIndex === ruleIndex ? { ...rule, ...patch } : rule)));
  };
  const removeRule = (groupIndex: number, ruleIndex: number) => {
    onChange(groups.map((group, gIndex) => gIndex !== groupIndex ? group : group.filter((_, rIndex) => rIndex !== ruleIndex)).filter((group) => group.length > 0));
  };
  if (!others.length) {
    return <p className="fs-field-helper">{t("react.workflows.operator.condition_no_other_fields", "There is no other field to depend on.")}</p>;
  }
  return (
    <div className="space-y-3" data-testid="operator-conditional-logic">
      {field.required ? <p className="fs-field-helper">{t("react.workflows.operator.condition_required_note", "This field is required in the contract, so it cannot depend on a condition. Make it optional in the contract first.")}</p> : null}
      <IssueLines issues={issues} />
      {groups.length ? null : <p className="fs-field-helper">{t("react.workflows.operator.condition_always", "Always shown.")}</p>}
      {groups.map((group, groupIndex) => (
        <div className="space-y-2" key={`group-${groupIndex}`}>
          {groupIndex > 0 ? <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-tertiary">{t("react.workflows.operator.condition_or", "or")}</p> : null}
          <fieldset className="space-y-2 rounded-lg border border-default p-2">
            <legend className="px-1 text-xs fs-ink-secondary">{t("react.workflows.operator.condition_show_if", "Show this field if")}</legend>
            {group.map((rule, ruleIndex) => {
              const target = others.find((other) => other.name === rule.field);
              const number = `${groupIndex + 1}.${ruleIndex + 1}`;
              const operators = operatorsFor(rule.field);
              return (
                <div className="grid items-center gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]" key={`rule-${groupIndex}-${ruleIndex}`}>
                  <DsSelect
                    ariaLabel={t("react.workflows.operator.condition_field", "Rule {number}: field", { number })}
                    onChange={(name) => {
                      const nextOperators = operatorsFor(name);
                      const operator = nextOperators.includes(rule.operator) ? rule.operator : nextOperators[0] ?? "==";
                      updateRule(groupIndex, ruleIndex, { field: name, operator, value: operator.endsWith("empty") ? null : "" });
                    }}
                    options={others.map((other) => ({ value: other.name, label: other.label || other.name }))}
                    value={rule.field}
                  />
                  <DsSelect
                    ariaLabel={t("react.workflows.operator.condition_operator", "Rule {number}: operator", { number })}
                    onChange={(operator) => updateRule(groupIndex, ruleIndex, { operator, value: operator.endsWith("empty") ? null : rule.value })}
                    options={operators.map((operator) => ({ value: operator, label: operatorLabel(operator, t) }))}
                    value={rule.operator}
                  />
                  <ConditionValueControl
                    kind={target?.kind ?? "string"}
                    label={t("react.workflows.operator.condition_value", "Rule {number}: value", { number })}
                    onChange={(value) => updateRule(groupIndex, ruleIndex, { value })}
                    optionLabels={target?.option_labels ?? {}}
                    options={target?.options ?? []}
                    rule={rule}
                  />
                  <Button aria-label={t("react.workflows.operator.condition_remove", "Remove rule {number}", { number })} onClick={() => removeRule(groupIndex, ruleIndex)} size="sm" type="button" variant="ghost-danger">
                    <Trash2 aria-hidden="true" />
                  </Button>
                </div>
              );
            })}
            <Button onClick={() => onChange(groups.map((item, index) => index === groupIndex ? [...item, newRule()] : item))} size="sm" type="button" variant="ghost">
              <Plus aria-hidden="true" />{t("react.workflows.operator.condition_and", "and")}
            </Button>
          </fieldset>
        </div>
      ))}
      <Button onClick={() => onChange([...groups, [newRule()]])} size="sm" type="button" variant="secondary">
        <Plus aria-hidden="true" />{t("react.workflows.operator.condition_add_group", "Add rule group")}
      </Button>
    </div>
  );
}

const TEXT_ENTRY_KINDS = new Set(["string", "number"]);

function TextSetting({ label, value, onChange, placeholder, multiline = false }: Readonly<{ label: string; value: string; onChange: (value: string) => void; placeholder?: string; multiline?: boolean }>) {
  return (
    <label className="block text-xs fs-ink-secondary">
      <span>{label}</span>
      {multiline
        ? <textarea className="fs-textarea mt-1 w-full" onChange={(event) => onChange(event.target.value)} placeholder={placeholder} rows={2} value={value} />
        : <input className="fs-input mt-1 w-full" onChange={(event) => onChange(event.target.value)} placeholder={placeholder} value={value} />}
    </label>
  );
}

export type FieldListState = {
  openKey: string | null;
  setOpenKey: (key: string | null) => void;
  fieldTabs: Record<string, OperatorFieldTab>;
  setFieldTab: (key: string, tab: OperatorFieldTab) => void;
};

type FieldListProps = Readonly<{
  fields: readonly FormFieldView[];
  configs: Record<string, OperatorFieldConfig>;
  onConfig: (name: string, next: OperatorFieldConfig) => void;
  /** Moves a top-level field in the form order; sub-fields keep the contract's order. */
  onMove?: (name: string, delta: number) => void;
  /** Contract nodes of this level, for the contract's title, description and default. */
  nodes: Record<string, Record<string, unknown>>;
  parents: readonly string[];
  issues: readonly OperatorIssue[];
  compatibility: OperatorCompatibility;
  state: FieldListState;
  /** Whether the jobs list shows or filters a top-level input. */
  listUsage?: (name: string) => { column: boolean; filter: boolean };
}>;

function hasContractDefault(node: Record<string, unknown>): boolean {
  return node.default !== undefined || node["x-fs-default-configured"] === true;
}

function FieldPanel({
  field,
  config,
  node,
  siblings,
  keyPath,
  props,
}: Readonly<{ field: FormFieldView; config: OperatorFieldConfig; node: Record<string, unknown>; siblings: readonly FormFieldView[]; keyPath: string[]; props: FieldListProps }>) {
  const { t } = useI18n();
  const idPrefix = useId();
  const [widgetOpen, setWidgetOpen] = useState(false);
  const { compatibility, issues, onConfig, state } = props;
  const key = keyPath.join("/");
  const tab = state.fieldTabs[key] ?? "general";
  const prefix = fieldPath(keyPath);
  const own = ownFieldIssues(issues, prefix);
  const set = (patch: Partial<OperatorFieldConfig>) => onConfig(field.name, { ...config, ...patch });
  const widgets = field.widgets ?? [];
  const widget = config.widget && widgets.includes(config.widget) ? config.widget : widgets[0] ?? "";
  const placed = new Set<OperatorIssue>([
    ...issuesAtKey(own, prefix, "widget"),
    ...issuesAtKey(own, prefix, "hidden"),
    ...issuesAtKey(own, prefix, "conditional_logic"),
    ...issuesAtKey(own, prefix, "option_labels"),
  ]);
  const elsewhere = own.filter((issue) => !placed.has(issue));
  const requiredWithoutDefault = field.required && !field.fixed && !hasContractDefault(node);
  const tabs = [
    { key: "general", label: t("react.workflows.operator.field_tab_general", "General") },
    { key: "presentation", label: t("react.workflows.operator.field_tab_presentation", "Presentation") },
    { key: "conditions", label: t("react.workflows.operator.field_tab_conditions", "Conditional logic") },
  ];
  const subFields = field.sub_fields ?? [];
  const subNodes = schemaProperties(field.kind === "object_list" ? node.items : node);
  let content: ReactNode;
  if (tab === "presentation") {
    content = (
      <div className="space-y-3">
        <TextSetting label={t("react.workflows.operator.instructions", "Instructions")} multiline onChange={(instructions) => set({ instructions })} placeholder={String(node.description ?? "")} value={config.instructions} />
        {TEXT_ENTRY_KINDS.has(field.kind) ? (
          <div className="grid gap-2 sm:grid-cols-3">
            <TextSetting label={t("react.workflows.operator.placeholder", "Placeholder")} onChange={(placeholder) => set({ placeholder })} value={config.placeholder} />
            <TextSetting label={t("react.workflows.operator.prepend", "Prepend")} onChange={(prepend) => set({ prepend })} value={config.prepend} />
            <TextSetting label={t("react.workflows.operator.append", "Append")} onChange={(append) => set({ append })} value={config.append} />
          </div>
        ) : null}
        <label className="block text-xs fs-ink-secondary">
          <span>{t("react.workflows.operator.width", "Width, % of the form row")}</span>
          <input className="fs-input mt-1 w-32" max={100} min={10} onChange={(event) => {
            const value = event.target.value.trim();
            const parsed = Number(value);
            set({ width: value && Number.isFinite(parsed) ? Math.round(parsed) : null });
          }} placeholder="100" type="number" value={config.width ?? ""} />
        </label>
        {field.options?.length ? (
          <div className="space-y-1" data-testid="operator-option-labels">
            <p className="text-xs fs-ink-secondary">{t("react.workflows.operator.option_labels", "Option labels")}</p>
            <p className="fs-field-helper">{t("react.workflows.operator.option_labels_help", "The values come from the contract; the operator sees the labels.")}</p>
            {field.options.map((option) => (
              <div className="grid items-center gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]" key={option}>
                <code className="fs-value-chip">{option}</code>
                <input aria-label={t("react.workflows.operator.option_label_for", "Label of {option}", { option })} className="fs-input w-full" onChange={(event) => set({ option_labels: { ...config.option_labels, [option]: event.target.value } })} placeholder={option} value={config.option_labels[option] ?? ""} />
              </div>
            ))}
            <IssueLines issues={issuesAtKey(own, prefix, "option_labels")} />
          </div>
        ) : null}
        {field.kind === "object_list" ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.operator.layout", "Layout")}</span>
              <DsSelect ariaLabel={t("react.workflows.operator.layout", "Layout")} className="mt-1" onChange={(layout) => set({ layout: layout as OperatorFieldLayout })} options={[
                { value: "table", label: t("react.workflows.operator.layout_table", "Table") },
                { value: "block", label: t("react.workflows.operator.layout_block", "Block") },
                { value: "row", label: t("react.workflows.operator.layout_row", "Row") },
              ]} value={config.layout || "table"} />
            </label>
            <TextSetting label={t("react.workflows.operator.button_label", "Add button label")} onChange={(button_label) => set({ button_label })} placeholder={t("react.workflows.operator.button_label_default", "Add row")} value={config.button_label} />
          </div>
        ) : null}
        {subFields.length ? (
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.sub_fields", "Sub-fields")}</p>
            <OperatorFieldList
              compatibility={compatibility}
              configs={config.sub_fields}
              fields={subFields}
              issues={issues}
              nodes={subNodes}
              onConfig={(name, next) => set({ sub_fields: { ...config.sub_fields, [name]: next } })}
              parents={keyPath}
              state={state}
            />
          </div>
        ) : null}
      </div>
    );
  } else if (tab === "conditions") {
    content = (
      <ConditionalLogicEditor
        compatibility={compatibility}
        config={config}
        field={field}
        issues={issuesAtKey(own, prefix, "conditional_logic")}
        onChange={(conditional_logic) => set({ conditional_logic })}
        siblings={siblings}
      />
    );
  } else {
    content = (
      <div className="space-y-3">
        <dl className="grid gap-x-3 gap-y-1 text-xs sm:grid-cols-[auto_1fr]">
          <dt className="fs-ink-tertiary">{t("react.workflows.operator.field_key", "Key")}</dt><dd><code className="fs-value-chip">{field.name}</code></dd>
          <dt className="fs-ink-tertiary">{t("react.workflows.operator.field_type", "Type")}</dt><dd>{kindLabel(field.kind, t)}{field.format ? ` · ${field.format}` : ""}</dd>
          <dt className="fs-ink-tertiary">{t("react.workflows.operator.field_required", "Required")}</dt><dd>{field.required ? t("react.workflows.operator.yes", "Yes") : t("react.workflows.operator.no", "No")}</dd>
          {field.options?.length ? <><dt className="fs-ink-tertiary">{t("react.workflows.options", "Options")}</dt><dd>{field.options.join(", ")}</dd></> : null}
        </dl>
        <p className="fs-field-helper">{t("react.workflows.operator.contract_owns_fields", "Key, type, requirement and options come from the workflow contract. Change them in Smart setup.")}</p>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="fs-ink-secondary">{t("react.workflows.operator.widget", "Widget")}:</span>
          <strong>{widget ? widgetLabel(widget, t) : t("react.workflows.operator.widget_none", "None fits this type")}</strong>
          {widgets.length ? <Button onClick={() => setWidgetOpen(true)} size="sm" type="button" variant="secondary">{t("react.workflows.operator.choose_widget", "Choose widget")}</Button> : null}
        </div>
        <IssueLines issues={issuesAtKey(own, prefix, "widget")} />
        <TextSetting label={t("react.workflows.label", "Label")} onChange={(label) => set({ label })} placeholder={String(node.title ?? "") || field.name} value={config.label} />
        <div className="space-y-1">
          <div className="flex items-center gap-2 text-xs">
            <DsSwitch checked={config.hidden} label={t("react.workflows.operator.hidden", "Hidden from the operator")} onCheckedChange={(hidden) => set({ hidden })} />
            <span>{t("react.workflows.operator.hidden", "Hidden from the operator")}</span>
          </div>
          <p className="fs-field-helper">{field.fixed
            ? t("react.workflows.operator.fixed_note", "The contract fixes this value, so the form never shows it.")
            : t("react.workflows.operator.hidden_help", "A hidden field is never sent from the form; the contract's default applies.")}</p>
          {config.hidden && requiredWithoutDefault ? <p className="fs-field-helper error">{t("react.workflows.operator.hidden_required_warning", "Required in the contract without a default: publishing is blocked while it is hidden.")}</p> : null}
          <IssueLines issues={issuesAtKey(own, prefix, "hidden")} />
        </div>
        {props.listUsage && keyPath.length === 1 ? (() => {
          const usage = props.listUsage(field.name);
          return usage.column || usage.filter ? <p className="flex gap-2 text-xs">
            {usage.column ? <DsBadge>{t("react.workflows.operator.used_as_column", "Column in the list")}</DsBadge> : null}
            {usage.filter ? <DsBadge>{t("react.workflows.operator.used_as_filter", "Filter in the list")}</DsBadge> : null}
          </p> : null;
        })() : null}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-testid={`operator-field-panel-${key}`}>
      <IssueLines issues={elsewhere} />
      <DsTabs active={tab} ariaLabel={t("react.workflows.operator.field_settings", "Settings of {name}", { name: field.label || field.name })} idPrefix={idPrefix} items={tabs} onSelect={(next) => state.setFieldTab(key, next as OperatorFieldTab)} />
      <div aria-labelledby={`${idPrefix}-tab-${tab}`} id={`${idPrefix}-panel-${tab}`} role="tabpanel">{content}</div>
      {widgetOpen ? (
        <WidgetDialog
          current={widget}
          field={field}
          onChoose={(chosen) => {
            setWidgetOpen(false);
            set({ widget: chosen === widgets[0] ? "" : chosen });
          }}
          onClose={() => setWidgetOpen(false)}
        />
      ) : null}
    </div>
  );
}

/** Up and down for one item of an ordered list; the end it sits at is disabled. */
export function MoveButtons({ index, count, name, onMove }: Readonly<{ index: number; count: number; name: string; onMove: (delta: number) => void }>) {
  const { t } = useI18n();
  return (
    <span className="inline-flex gap-1">
      <IconButton disabled={index === 0} icon={ArrowUp} label={t("react.workflows.report_move_up", "Move {name} up", { name })} onClick={() => onMove(-1)} size="small" />
      <IconButton disabled={index === count - 1} icon={ArrowDown} label={t("react.workflows.report_move_down", "Move {name} down", { name })} onClick={() => onMove(1)} size="small" />
    </span>
  );
}

/**
 * The form's fields as an ACF-style list: one line per field (label, key and
 * type, visibility, widget), opening into General, Presentation and
 * Conditional logic. Group and repeater sub-fields use the same list.
 */
export function OperatorFieldList(props: FieldListProps) {
  const { t } = useI18n();
  const { fields, configs, onMove, parents, issues, state } = props;
  const openId = fields.map((field) => [...parents, field.name].join("/"))
    .find((id) => state.openKey === id || state.openKey?.startsWith(`${id}/`)) ?? null;
  const items = fields.map((field, index) => {
    const keyPath = [...parents, field.name];
    const id = keyPath.join("/");
    const config = configs[field.name] ?? emptyFieldConfig();
    const widgets = field.widgets ?? [];
    const widget = config.widget && widgets.includes(config.widget) ? config.widget : widgets[0] ?? "";
    const hidden = config.hidden || Boolean(field.fixed);
    const fieldIssues = ownFieldIssues(issues, fieldPath(keyPath));
    const errors = fieldIssues.filter((issue) => issue.severity !== "warning").length;
    const label = config.label || String(props.nodes[field.name]?.title ?? "") || field.name;
    return {
      id,
      header: (
        <span className="flex min-w-0 flex-wrap items-center gap-2" data-testid={`operator-field-row-${id}`}>
          <span className="font-medium">{label}</span>
          <span className="font-mono text-xs fs-ink-tertiary">{field.name} · {kindLabel(field.kind, t)}</span>
          {field.required ? <DsBadge tone="orange">{t("react.workflows.operator.required_badge", "Required")}</DsBadge> : null}
          {hidden ? <DsBadge>{field.fixed ? t("react.workflows.operator.fixed_badge", "Fixed by contract") : t("react.workflows.operator.hidden_badge", "Hidden")}</DsBadge> : null}
          {config.conditional_logic.length ? <DsBadge>{t("react.workflows.operator.conditional_badge", "Conditional")}</DsBadge> : null}
          {widget ? <span className="text-xs fs-ink-secondary">{widgetLabel(widget, t)}</span> : null}
          {errors ? <DsBadge tone="danger">{t("react.workflows.operator.issue_count", "Issues: {count}", { count: errors })}</DsBadge> : null}
        </span>
      ),
      accessory: onMove ? (
        <MoveButtons count={fields.length} index={index} name={label} onMove={(delta) => onMove(field.name, delta)} />
      ) : undefined,
      accessorySlot: "icon-pair" as const,
      panel: openId === id ? (
        <FieldPanel config={config} field={field} keyPath={keyPath} node={props.nodes[field.name] ?? {}} props={props} siblings={fields} />
      ) : null,
    };
  });
  if (!fields.length) return <p className="workflow-contract-empty">{t("react.workflows.operator.no_fields", "The contract declares no fields the form can show.")}</p>;
  return <DsAccordion items={items} onOpenChange={(id) => state.setOpenKey(id ?? (parents.length ? parents.join("/") : null))} openId={openId} />;
}

