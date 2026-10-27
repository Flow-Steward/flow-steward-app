import { useId, type ReactElement, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";

import "./ds-accordion.css";

export type DsAccordionItem = { id: string; header: ReactNode; panel: ReactNode; accessory?: ReactNode };
export type DsAccordionProps = {
  items: ReadonlyArray<DsAccordionItem>;
  openId: string | null;
  onOpenChange: (id: string | null) => void;
};

/** Single-open disclosure. An interactive accessory is a sibling of the trigger. */
export function DsAccordion({ items, openId, onOpenChange }: Readonly<DsAccordionProps>): ReactElement {
  const namespace = useId();
  return <div className="fs-accordion-list">
    {items.map(({ id, header, panel, accessory }) => {
      const open = id === openId;
      const triggerId = `${namespace}-trigger-${encodeURIComponent(id)}`;
      const panelId = `${namespace}-panel-${encodeURIComponent(id)}`;
      return <section className="fs-accordion-item" data-open={open} key={id}>
        <div className="fs-accordion-header" data-has-accessory={accessory != null}>
          <button aria-controls={panelId} aria-expanded={open} className="fs-accordion-trigger" id={triggerId} onClick={() => onOpenChange(open ? null : id)} type="button">
            <span className="fs-accordion-question">{header}</span>
            <ChevronDown aria-hidden="true" />
          </button>
          {accessory != null ? <span className="fs-accordion-accessory">{accessory}</span> : null}
        </div>
        <div aria-labelledby={triggerId} className="fs-accordion-panel" hidden={!open} id={panelId} role="region">{panel}</div>
      </section>;
    })}
  </div>;
}
