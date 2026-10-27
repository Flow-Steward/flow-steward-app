import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { DsTable } from "@/components/shared/design-system";

import type { GridEntityRow } from "@/pages/registry/shared/catalog-surface-helpers";
import {
  badgeToneFor,
  columnsFromGridConfig,
  isRowActionVisible,
  resolveLinkHref,
  visibleRowActions,
} from "@/components/shared/data-grid-adapter";

const AGENTS_CONFIG = {
  columns: [
    { key: "display_name", label: "Agent", type: "link", link_href: "/app/registry/agents/{agent_id}" },
    { key: "_caps_text", label: "Skills", type: "text", truncate: true, truncate_width: "16rem" },
    { key: "_tools_count", label: "Tools", type: "text", width: "110px" },
    { key: "_contract", label: "Contract", type: "badge", badge_map: { loading: "neutral", ok: "green", error: "red" } },
  ],
  row_actions: [
    { action_id: "edit", label: "Edit", variant: "ghost" },
    { action_id: "toggle-on", label: "Enable", variant: "ghost", visible_if: { field: "enabled", eq: false } },
    { action_id: "toggle-off", label: "Disable", variant: "ghost", visible_if: { field: "enabled", eq: true } },
    { action_id: "delete", label: "Delete", variant: "danger" },
  ],
};

const ROW: GridEntityRow = {
  _entity_id: "planner agent",
  agent_id: "planner agent",
  display_name: "Planner",
  _caps_text: "planning, routing, review",
  _tools_count: "4",
  _contract: "error" as const,
  enabled: true,
};

describe("catalog grid adapter", () => {
  it("localizes the actions column at the real table render boundary", () => {
    const messages: Record<string, string> = { "monitoring.table.actions": "Veiksmai" };
    const columns = columnsFromGridConfig(AGENTS_CONFIG, () => <button type="button">Owner action</button>);
    render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "") => messages[key] || text }}><DsTable ariaLabel="Catalog" columns={columns} rowKey={(row) => String(row._entity_id)} rows={[ROW]} /></I18nContext.Provider>);
    expect(screen.getByRole("columnheader", { name: "Veiksmai" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Owner action" })).toBeInTheDocument();
  });
  it("localizes unavailable badge display without changing the technical row status", () => {
    const messages: Record<string, string> = { "react.shared.badge_unavailable": "neprieinama" };
    const config = { columns: [{ key: "status", label: "Status", type: "badge", badge_map: { unavailable: "neutral" } }] };
    const columns = columnsFromGridConfig(config, () => null);
    const row = { status: "unavailable" };
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "") => messages[key] || fallback || key }}>
        {columns[0].cell(row)}
      </I18nContext.Provider>,
    );
    expect(screen.getByText("neprieinama")).toHaveClass("fs-badge");
    expect(badgeToneFor(config.columns[0].badge_map, row.status)).toBe("neutral");
    expect(row.status).toBe("unavailable");
  });

  it("localizes known badge labels without changing row values, filtering or unknown operator content", () => {
    const messages: Record<string, string> = { "react.shared.badge_enabled": "įjungta" };
    const config = { columns: [{ key: "status", label: "Status", type: "badge", badge_map: { enabled: "green" } }] };
    const columns = columnsFromGridConfig(config, () => null);
    const row = { status: "enabled" };
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "") => messages[key] || fallback || key }}>
        {columns[0].cell(row)}
        {columns[0].cell({ status: "operator-status-42" })}
      </I18nContext.Provider>,
    );
    expect(screen.getByText("įjungta")).toHaveClass("fs-badge", "green");
    expect(screen.getByText("operator-status-42")).toBeInTheDocument();
    expect(row.status).toBe("enabled");
    expect(isRowActionVisible({ action_id: "edit", label: "Edit", visible_if: { field: "status", eq: "enabled" } }, row)).toBe(true);
  });

  it("maps the config's tone names onto the published badge families", () => {
    const map = { ok: "green", error: "red", loading: "neutral" };
    expect(badgeToneFor(map, "ok")).toBe("green");
    expect(badgeToneFor(map, "error")).toBe("danger");
    expect(badgeToneFor(map, "loading")).toBe("neutral");
    // An unmapped value must not silently borrow another state's colour.
    expect(badgeToneFor(map, "unknown")).toBe("neutral");
  });

  it("substitutes row values into a link template and escapes them", () => {
    expect(resolveLinkHref("/app/registry/agents/{agent_id}", ROW)).toBe(
      "/app/registry/agents/planner%20agent",
    );
  });

  it("keeps the config's visible_if gating for row actions", () => {
    expect(isRowActionVisible({ action_id: "edit", label: "Edit" }, ROW)).toBe(true);
    expect(
      isRowActionVisible({ action_id: "toggle-off", label: "Disable", visible_if: { field: "enabled", eq: true } }, ROW),
    ).toBe(true);
    expect(
      isRowActionVisible({ action_id: "toggle-on", label: "Enable", visible_if: { field: "enabled", eq: false } }, ROW),
    ).toBe(false);
    expect(visibleRowActions(AGENTS_CONFIG, ROW).map((action) => action.action_id)).toEqual([
      "edit",
      "toggle-off",
      "delete",
    ]);
  });

  it("builds table columns that render links, badges and truncation from the config", () => {
    const columns = columnsFromGridConfig(AGENTS_CONFIG, () => <button type="button">Actions</button>);

    expect(columns.map((column) => column.key)).toEqual([
      "display_name",
      "_caps_text",
      "_tools_count",
      "_contract",
      "actions",
    ]);
    expect(columns[1].truncate).toBe("16rem");
    expect(columns[1].tooltip?.(ROW)).toBe("planning, routing, review");
    expect(columns[2].width).toBe(110);

    render(
      <MemoryRouter>
        <div>
          {columns[0].cell(ROW)}
          {columns[3].cell(ROW)}
        </div>
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Planner" })).toHaveAttribute(
      "href",
      "/app/registry/agents/planner%20agent",
    );
    expect(screen.getByText("error")).toHaveClass("fs-badge", "danger");
  });

  it("omits the actions column when the config declares none", () => {
    const columns = columnsFromGridConfig({ columns: AGENTS_CONFIG.columns, row_actions: [] }, () => null);
    expect(columns.some((column) => column.key === "actions")).toBe(false);
  });
});
