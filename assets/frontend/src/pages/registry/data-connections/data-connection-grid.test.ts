import { describe, expect, it } from "vitest";

import {
  buildConnectionRows,
  dataConnectionGridConfig,
  healthLabel,
  sourceLabel,
} from "./data-connection-grid";

const context = {
  workspaceById: new Map([["ws-1", "Default workspace"]]),
  connectorSourceLabelByLocalId: { toolbox_database: "Toolbox database" },
};

describe("dataConnectionGridConfig", () => {
  it("keeps the six data columns and the published badge tones", () => {
    const config = dataConnectionGridConfig();
    const columns = config.columns as Array<Record<string, unknown>>;

    expect(columns.map((column) => column.label)).toEqual([
      "Connection",
      "Workspace",
      "Source",
      "Environment",
      "Health",
      "Last checked",
    ]);
    const health = columns.find((column) => column.key === "_health_label");
    expect(health?.type).toBe("badge");
    expect(health?.badge_map).toEqual({
      healthy: "green",
      unhealthy: "danger",
      unknown: "neutral",
      disabled: "neutral",
    });
  });

  it("drops row actions for a read-only operator", () => {
    expect(dataConnectionGridConfig(true).row_actions).toEqual([]);
    expect(dataConnectionGridConfig(false).row_actions).toHaveLength(5);
  });
});

describe("buildConnectionRows", () => {
  it("labels a disabled connection as disabled rather than by its health", () => {
    expect(healthLabel({ enabled: false, health_status: "healthy" } as never)).toBe("disabled");
    expect(healthLabel({ enabled: true, health_status: "healthy" } as never)).toBe("healthy");
    expect(healthLabel({ enabled: true } as never)).toBe("unknown");
  });

  it("prefers the connector catalog label over the stored source kind", () => {
    expect(
      sourceLabel(
        {
          connector_ref_jsonb: { local_connector_id: "toolbox_database" },
          source_kind: "postgres",
        } as never,
        context.connectorSourceLabelByLocalId,
      ),
    ).toBe("Toolbox database");
    expect(
      sourceLabel({ source_kind: "postgres" } as never, context.connectorSourceLabelByLocalId),
    ).toBe("postgres");
    expect(sourceLabel({} as never, {})).toBe("extension");
  });

  it("falls back through the three workspace names the page has always used", () => {
    const [fromPayload, fromMap, fromId] = buildConnectionRows(
      [
        { connection_id: "a", workspace_id: "ws-1", workspace_display_name: "Payload name" },
        { connection_id: "b", workspace_id: "ws-1" },
        { connection_id: "c", workspace_id: "ws-9" },
      ] as never,
      context,
    );

    expect(fromPayload._workspace_label).toBe("Payload name");
    expect(fromMap._workspace_label).toBe("Default workspace");
    expect(fromId._workspace_label).toBe("ws-9");
  });

  it("joins tags and leaves the cell empty when there are none", () => {
    const [tagged, bare] = buildConnectionRows(
      [
        { connection_id: "a", tags: ["warehouse", "reporting"] },
        { connection_id: "b", tags: [] },
      ] as never,
      context,
    );

    expect(tagged._tags_label).toBe("warehouse, reporting");
    expect(bare._tags_label).toBe("");
  });
});
