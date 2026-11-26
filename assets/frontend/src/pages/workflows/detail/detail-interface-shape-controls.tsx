import { useI18n } from "@/lib/i18n/useI18n";
import { localizedWORKFLOW_INTERFACE_FIELD_TYPES, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import {
  WORKFLOW_ARRAY_ITEM_TYPES,
  WORKFLOW_FIELD_FORMATS,
  type WorkflowInterfaceFieldDraft,
} from "@/pages/workflows/detail/detail-interface";

const CONTROL_CLASS = "fs-input mt-1 w-full";
const PROPERTIES_PLACEHOLDER = '[{"name": "city", "type": "string", "required": true}]';

type ShapeControlProps = {
  controlId: string;
  controlName: string;
  direction: "input" | "output";
  row: WorkflowInterfaceFieldDraft;
  updateRow: (patch: Partial<WorkflowInterfaceFieldDraft>) => void;
};

function FormatControl({ controlId, controlName, row, updateRow }: Readonly<ShapeControlProps>) {
  const { t } = useI18n();
  const labels: Record<(typeof WORKFLOW_FIELD_FORMATS)[number], string> = {
    date: t("react.workflows.contract_format_date", "Date"),
    "date-time": t("react.workflows.contract_format_date_time", "Date and time"),
    time: t("react.workflows.contract_format_time", "Time"),
    email: t("react.workflows.contract_format_email", "Email address"),
    uri: t("react.workflows.contract_format_uri", "URI"),
  };
  return (
    <label className="block text-xs text-[var(--fs-text-primary)]">
      <span>{t("react.workflows.contract_format", "Format")}</span>
      <select className={CONTROL_CLASS} id={`${controlId}-format`} name={`${controlName}_format`} onChange={(event) => updateRow({ format: event.target.value })} value={textOrEmpty(row.format)}>
        <option value="">{t("react.workflows.contract_format_any", "Any text")}</option>
        {WORKFLOW_FIELD_FORMATS.map((format) => <option key={format} value={format}>{labels[format]}</option>)}
      </select>
    </label>
  );
}

function PropertiesControl({ controlId, controlName, label, row, updateRow }: Readonly<ShapeControlProps & { label: string }>) {
  const { t } = useI18n();
  return (
    <div>
      <label className="block text-xs text-[var(--fs-text-primary)]">
        <span>{label}</span>
        <textarea
          aria-describedby={`${controlId}-properties-help`}
          className={`${CONTROL_CLASS} font-mono`}
          id={`${controlId}-properties`}
          name={`${controlName}_properties`}
          onChange={(event) => updateRow({ propertiesText: event.target.value })}
          placeholder={PROPERTIES_PLACEHOLDER}
          rows={3}
          value={row.propertiesText ?? ""}
        />
      </label>
      <p className="fs-field-helper mt-1" id={`${controlId}-properties-help`}>
        {t("react.workflows.contract_properties_help", "A JSON list of fields: name, type, required, options, format. Groups nest up to 3 levels.")}
      </p>
    </div>
  );
}

function ArrayShapeControls(props: Readonly<ShapeControlProps>) {
  const { t } = useI18n();
  const { controlId, controlName, direction, row, updateRow } = props;
  const itemType = textOrEmpty(row.itemType);
  // An output never returns files through a variable: files are returned by steps.
  const itemTypes = localizedWORKFLOW_INTERFACE_FIELD_TYPES(t).filter((option) =>
    (WORKFLOW_ARRAY_ITEM_TYPES as readonly string[]).includes(option.value) && (direction === "input" || option.value !== "file"),
  );
  return (
    <>
      <div className="grid gap-2 sm:grid-cols-3">
        <label className="block text-xs text-[var(--fs-text-primary)]">
          <span>{t("react.workflows.contract_item_type", "Element type")}</span>
          <select className={CONTROL_CLASS} id={`${controlId}-item-type`} name={`${controlName}_item_type`} onChange={(event) => updateRow({ itemType: event.target.value })} value={itemType}>
            <option value="">{t("react.workflows.contract_item_type_any", "Any value")}</option>
            {itemTypes.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        <label className="block text-xs text-[var(--fs-text-primary)]">
          <span>{t("react.workflows.contract_min_items", "Minimum items")}</span>
          <input className={CONTROL_CLASS} id={`${controlId}-min-items`} inputMode="numeric" min={0} name={`${controlName}_min_items`} onChange={(event) => updateRow({ minItems: event.target.value })} type="number" value={row.minItems ?? ""} />
        </label>
        <label className="block text-xs text-[var(--fs-text-primary)]">
          <span>{t("react.workflows.contract_max_items", "Maximum items")}</span>
          <input className={CONTROL_CLASS} id={`${controlId}-max-items`} inputMode="numeric" min={0} name={`${controlName}_max_items`} onChange={(event) => updateRow({ maxItems: event.target.value })} type="number" value={row.maxItems ?? ""} />
        </label>
      </div>
      {itemType === "select" ? (
        <label className="block text-xs text-[var(--fs-text-primary)]">
          <span>{t("react.workflows.contract_item_options", "Element options")}</span>
          <input className={CONTROL_CLASS} id={`${controlId}-item-options`} name={`${controlName}_item_options`} onChange={(event) => updateRow({ itemOptions: event.target.value })} placeholder="contract, invoice" value={row.itemOptions ?? ""} />
        </label>
      ) : null}
      {itemType === "object" ? (
        <PropertiesControl {...props} label={t("react.workflows.contract_item_properties", "Element properties (JSON)")} />
      ) : null}
    </>
  );
}

/**
 * The declared shape of a field (ADR 0075): a text format, the element and
 * item counts of an array, the properties of an object. Every part is optional;
 * a field without one validates as before.
 */
export function FieldShapeControls(props: Readonly<ShapeControlProps>) {
  const { t } = useI18n();
  const type = textOrEmpty(props.row.type) || "string";
  if (type === "string" || type === "paragraph") return <FormatControl {...props} />;
  if (type === "array") return <ArrayShapeControls {...props} />;
  if (type === "object") return <PropertiesControl {...props} label={t("react.workflows.contract_properties", "Properties (JSON)")} />;
  return null;
}
