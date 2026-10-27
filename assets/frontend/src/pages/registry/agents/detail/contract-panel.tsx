import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { DsBadge, type DsBadgeTone } from "@/components/shared/design-system";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/**
 * The Agents-only Source attribute. Colour used to carry the distinction —
 * violet for a skill, amber for a tool — and `DsBadgeTone` publishes neither.
 * The words carry it now, and the tone stays neutral because inheriting a field
 * is normal, not a status.
 */
function describeContractSource(raw: string, t: I18nContextValue["t"] = fallbackContext.t): {
  label: string;
  detail: string;
  inherited: boolean;
  tone: DsBadgeTone;
} {
  const value = (raw || "").trim();
  if (!value || value === "own" || value === "—" || value === "agent") {
    return { label: t("react.registry.own", "Own"), detail: "", inherited: false, tone: "neutral" };
  }
  const sep = value.indexOf(":");
  const kind = sep >= 0 ? value.slice(0, sep) : value;
  const id = sep >= 0 ? value.slice(sep + 1) : "";
  if (kind === "skill") {
    return { label: t("react.registry.from_skill", "From skill"), detail: id, inherited: true, tone: "neutral" };
  }
  if (kind === "tool") {
    return { label: t("react.registry.from_tool", "From tool"), detail: id, inherited: true, tone: "neutral" };
  }
  return { label: value, detail: "", inherited: true, tone: "neutral" };
}

/**
 * Declared here rather than imported: the Tools page keeps its own copy local
 * too, and promoting it to the shared layer is a separate change with its own
 * blast radius.
 */
function RequiredMark() {
  const { t } = useI18n();
  return (
    <span
      aria-label={t("registry.workflow_detail.outputs.required_badge", "required")}
      className="fs-param-required"
      data-testid="contract-required-badge"
    >
      *
    </span>
  );
}

function SourceBadge({ source }: Readonly<{ source: string }>) {
  const { t } = useI18n();
  const info = describeContractSource(source, t);
  // The hook sits on a wrapper because `DsBadge` publishes no `testId` prop and
  // adding one is a change to the shared layer, which this transfer excludes.
  return (
    <span data-testid="contract-source-badge">
      <DsBadge tone={info.tone}>{info.label}</DsBadge>
      {info.detail ? <span className="fs-table-mono"> {info.detail}</span> : null}
    </span>
  );
}

export function ContractPanel({
  title,
  rows,
  emptyMessage,
}: Readonly<{
  title: string;
  rows: Array<Record<string, unknown>>;
  emptyMessage: string;
}>) {
  const { t } = useI18n();
  const fields = rows.map((row, index) => ({
    description: firstStringFromUnknownValue([row.description, row.notes, ""]).trim(),
    name: firstStringFromUnknownValue([row.name, row.output, row.field, `row_${index + 1}`]),
    required: row.required === true,
    source: firstStringFromUnknownValue([row.source, row.from, "—"]),
    type: firstStringFromUnknownValue([row.type, row.kind, row.value_type, "text"]),
  }));
  // The same switch the Tools page uses: a chip cannot carry prose, and here the
  // Source attribute is prose too, so it is always present and the row form
  // always wins. The chip branch stays for a contract that declares neither.
  const asRows = fields.some((field) => field.description || field.source);

  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-field p-3">
      <span className="fs-field-label">{title}</span>
      {fields.length && asRows ? (
          <div className="fs-signature">
            <div className="fs-signature-head">
              <span>{t("react.registry.parameter", "Parameter")}</span>
              <span>{t("trash.columns.type", "Type")}</span>
              <span>{t("registry.workflows.simulation.description", "Description")}</span>
            </div>
            {fields.map((field, index) => (
              <div
                className="fs-signature-row"
                data-testid="contract-detail-row"
                key={`${title}:${field.name}:${index}`}
              >
                <span className="fs-signature-name">
                  {field.name}
                  {field.required ? <RequiredMark /> : null}
                </span>
                <span className="fs-signature-type">{field.type}</span>
                <span className="fs-signature-description">
                  {field.description}
                  {field.description ? " " : null}
                  <SourceBadge source={field.source} />
                </span>
              </div>
            ))}
          </div>
      ) : null}
      {fields.length && !asRows ? (
          <div className="fs-param-group">
            {fields.map((field, index) => (
              <span
                className="fs-param-chip"
                data-testid="contract-detail-row"
                key={`${title}:${field.name}:${index}`}
              >
                <span className="fs-param-name">
                  {field.name}
                  {field.required ? <RequiredMark /> : null}
                </span>
                <span className="fs-param-type">{field.type}</span>
              </span>
            ))}
          </div>
      ) : null}
      {!fields.length ? (
        <div className="fs-table-state">
          <strong>{emptyMessage}</strong>
        </div>
      ) : null}
      <p className="fs-param-legend">
        <span className="fs-param-required">*</span> {" "}{t("react.registry.required_unmarked_fields_are_optional", "required · unmarked fields are optional")}</p>
    </div>
  );
}
