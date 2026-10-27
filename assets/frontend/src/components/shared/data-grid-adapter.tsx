import { type DsBadgeTone, type DsTableColumn } from "@/components/shared/design-system";
import { LocalizedStatusBadge } from "@/components/shared/localized-status-badge";

import { normalizeString } from "@/pages/registry/shared/catalog-surface-helpers";
import { replaceDelimitedTokens } from "@/lib/utils/template-tokens";

type GridRow = Record<string, unknown>;

/**
 * Translates a declarative grid config into DsTable columns.
 *
 * `catalog-grid-config.ts` stays as it is: seven endpoint branches of plain
 * data, which is a reasonable way to describe seven similar inventories. Only
 * the renderer changes — from the retired web component to the published table
 * — so all seven registry pages move together without rewriting the config.
 */

export type CatalogColumnConfig = {
  key: string;
  label: string;
  type?: string;
  width?: string;
  sortable?: boolean;
  truncate?: boolean;
  truncate_width?: string;
  tooltip_key?: string;
  link_href?: string;
  badge_map?: Record<string, string>;
};

export type CatalogRowActionConfig = {
  action_id: string;
  label: string;
  variant?: string;
  /** The config gates on either an exact value or membership in a set. */
  visible_if?: { field: string; eq?: unknown; in?: readonly unknown[] };
};

// The config's tone names predate the published badge families.
const BADGE_TONES: Record<string, DsBadgeTone> = {
  green: "green",
  red: "danger",
  danger: "danger",
  accent: "orange",
  amber: "orange",
  orange: "orange",
  neutral: "neutral",
};

export function badgeToneFor(map: Record<string, string> | undefined, value: string): DsBadgeTone {
  return BADGE_TONES[normalizeString(map?.[value]).toLowerCase()] ?? "neutral";
}


/** `/app/registry/agents/{agent_id}` with row values substituted in. */
export function resolveLinkHref(template: string, row: GridRow): string {
  return replaceDelimitedTokens(
    template,
    "{",
    "}",
    (key) => encodeURIComponent(normalizeString(row[key])),
  );
}

export function isRowActionVisible(action: CatalogRowActionConfig, row: GridRow): boolean {
  const gate = action.visible_if;
  if (!gate) {
    return true;
  }
  const value = row[gate.field];
  if (Array.isArray(gate.in)) {
    return gate.in.includes(value);
  }
  if (Object.hasOwn(gate, "eq")) {
    return value === gate.eq;
  }
  // An unrecognised gate must hide the action, not expose it by accident.
  return false;
}

function renderCell(column: CatalogColumnConfig, row: GridRow) {
  const value = normalizeString(row[column.key]);
  if (column.type === "badge") {
    return value ? <LocalizedStatusBadge tone={badgeToneFor(column.badge_map, value)} value={value} /> : null;
  }
  if (column.type === "link" && column.link_href) {
    // link_href is already an app-absolute href built by appHref(), so this
    // stays a plain anchor. A router Link would prefix the basename a second
    // time and produce /app/app/... The row click still navigates client-side.
    return <a className="fs-link" href={resolveLinkHref(column.link_href, row)}>{value}</a>;
  }
  return value;
}

export function columnsFromGridConfig(
  config: Record<string, unknown>,
  renderActions: (row: GridRow) => React.ReactNode,
): Array<DsTableColumn<GridRow>> {
  const configured = Array.isArray(config.columns) ? (config.columns as CatalogColumnConfig[]) : [];
  const rowActions = Array.isArray(config.row_actions)
    ? (config.row_actions as CatalogRowActionConfig[])
    : [];

  const columns: Array<DsTableColumn<GridRow>> = configured.map((column) => {
    let tooltip: DsTableColumn<GridRow>["tooltip"];
    if (column.tooltip_key) {
      tooltip = (row) => normalizeString(row[column.tooltip_key as string]) || undefined;
    } else if (column.truncate) {
      tooltip = (row) => normalizeString(row[column.key]) || undefined;
    }
    return {
      key: column.key,
      header: column.label,
      sortable: column.sortable,
      width: column.width ? Number.parseInt(column.width, 10) || undefined : undefined,
      truncate: column.truncate ? column.truncate_width || "20rem" : undefined,
      tooltip,
      cell: (row) => renderCell(column, row),
    };
  });

  if (rowActions.length) {
    columns.push({
      key: "actions",
      header: "",
      ariaLabel: "Actions",
      cell: renderActions,
    });
  }
  return columns;
}

export function visibleRowActions(
  config: Record<string, unknown>,
  row: GridRow,
): CatalogRowActionConfig[] {
  const rowActions = Array.isArray(config.row_actions)
    ? (config.row_actions as CatalogRowActionConfig[])
    : [];
  return rowActions.filter((action) => isRowActionVisible(action, row));
}
