import { ChevronDown, Plus } from "lucide-react";
import { createContext, useContext, useId, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";

const ContractReadOnlyContext = createContext(false);

/** The contract shown to someone who may not edit it: every row is read open. */
export function ContractReadOnly({ value, children }: Readonly<{ value: boolean; children: ReactNode }>) {
  return <ContractReadOnlyContext.Provider value={value}>{children}</ContractReadOnlyContext.Provider>;
}

export type ContractBlockRow = {
  key: string;
  /** The row's name in the summary line, or what it is when it has no name. */
  name: string;
  /** No name yet: the summary shows the placeholder in the tertiary tone. */
  unnamed?: boolean;
  /** A prose title instead of a code identifier. */
  prose?: boolean;
  required?: boolean;
  /** The type as the schema spells it. */
  type?: string;
  /** The label or the description, one line. */
  summary?: string;
  /** A rejected value inside: the row stays open and its border says so. */
  invalid?: boolean;
  content: ReactNode;
};

/**
 * One block of the workflow contract (Expected inputs, Expected outputs,
 * Returned files): a header with the count, one summary line per row that
 * opens to edit it, the empty state, and Add at the bottom. One row is open at
 * a time; a row with a rejected value stays open so its error is in view. A
 * block without ``onAdd`` lists rows that are set elsewhere (returned files
 * are ticked on their step) and has no Add.
 */
export function ContractBlock({
  title,
  countText,
  note,
  emptyText,
  rows,
  addLabel,
  onAdd,
  testId,
}: Readonly<{
  title: string;
  countText: string;
  note?: ReactNode;
  emptyText: string;
  rows: ContractBlockRow[];
  addLabel?: string;
  onAdd?: () => void;
  testId?: string;
}>) {
  const { t } = useI18n();
  // A viewer reads the whole contract at once: every row open, nothing to add (the fields stay disabled).
  const readOnly = useContext(ContractReadOnlyContext);
  const idPrefix = useId();
  const [openKey, setOpenKey] = useState<string | null>(() => rows.find((row) => row.invalid)?.key ?? null);
  // Add opens the row it adds: the next render's last row, once it exists.
  const [openAdded, setOpenAdded] = useState(false);
  // A row opened by its rejected value stays open while the author fixes it.
  const firstInvalidKey = rows.find((row) => row.invalid)?.key ?? null;
  const [seenInvalidKey, setSeenInvalidKey] = useState(firstInvalidKey);
  if (seenInvalidKey !== firstInvalidKey) {
    setSeenInvalidKey(firstInvalidKey);
    if (firstInvalidKey) setOpenKey(firstInvalidKey);
  }
  const lastKey = rows.at(-1)?.key ?? null;
  const [seenLastKey, setSeenLastKey] = useState(lastKey);
  if (seenLastKey !== lastKey) {
    setSeenLastKey(lastKey);
    if (openAdded && lastKey) {
      setOpenAdded(false);
      setOpenKey(lastKey);
    }
  }

  return (
    <section aria-label={title} className="workflow-contract-block" data-testid={testId}>
      <div className="workflow-contract-block-head">
        <span className="workflow-contract-block-title">{title}</span>
        <span className="workflow-contract-block-count">{countText}</span>
      </div>
      {note ? <p className="fs-field-helper">{note}</p> : null}
      {rows.length ? (
        <div className="workflow-contract-rows">
          {rows.map((row) => {
            const open = readOnly || row.invalid || openKey === row.key;
            const bodyId = `${idPrefix}-${row.key}`;
            return (
              <div className="workflow-contract-row" data-invalid={row.invalid ? "true" : "false"} data-open={open ? "true" : "false"} key={row.key}>
                <button
                  aria-controls={open ? bodyId : undefined}
                  aria-expanded={open}
                  className="workflow-contract-row-summary"
                  onClick={() => setOpenKey(openKey === row.key ? null : row.key)}
                  type="button"
                >
                  <span className="workflow-contract-row-name" data-empty={row.unnamed ? "true" : "false"} data-prose={row.prose ? "true" : "false"}>{row.name}</span>
                  {row.required ? <span className="fs-required"><span aria-hidden="true">*</span><span className="fs-sr-only">{t("react.workflows.contract_required_legend", "required")}</span></span> : null}
                  {row.type ? <span className="workflow-contract-row-type">{row.type}</span> : null}
                  <span className="workflow-contract-row-desc">{row.summary}</span>
                  <ChevronDown aria-hidden="true" className="workflow-contract-row-chevron" />
                </button>
                {open ? <div className="workflow-contract-row-body" id={bodyId}>{row.content}</div> : null}
              </div>
            );
          })}
        </div>
      ) : <p className="workflow-contract-empty">{emptyText}</p>}
      {readOnly || !onAdd ? null : <div>
        <Button
          onClick={() => {
            setOpenAdded(true);
            onAdd();
          }}
          size="sm"
          type="button"
          variant="secondary"
        >
          <Plus aria-hidden="true" />{addLabel}
        </Button>
      </div>}
    </section>
  );
}

/** The legend under the contract blocks: the marker the summary lines use. */
export function ContractRequiredLegend() {
  const { t } = useI18n();
  return (
    <p className="fs-field-helper">
      <span aria-hidden="true" className="fs-required">*</span>{" "}{t("react.workflows.contract_required_legend", "required")}
    </p>
  );
}
