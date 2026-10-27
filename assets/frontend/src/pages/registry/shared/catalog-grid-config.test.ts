import { datasetGridConfig } from "@/pages/registry/datasets/dataset-grid";
import { buildGridConfig } from "@/pages/registry/shared/catalog-grid-config";
import { registryListPath } from "@/pages/registry/shared/catalog-surface-helpers";

type GridColumn = {
  type?: string;
  link_href?: string;
};

function firstLinkHref(config: Record<string, unknown>): string | undefined {
  const columns = Array.isArray(config.columns) ? config.columns as GridColumn[] : [];
  return columns.find((column) => column.type === "link")?.link_href;
}

describe("native registry grid links", () => {
  beforeEach(() => {
    vi.stubEnv("BASE_URL", "/app/");
    globalThis.history.replaceState({}, "", "/app/registry/agents");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    globalThis.history.replaceState({}, "", "/");
  });

  it.each([
    ["/api/agents", "agent_id"],
    ["/api/tools", "tool_id"],
    ["/api/skills", "skill_id"],
    ["/api/guardrails", "id"],
    ["/api/data-contracts", "contract_id"],
    ["/api/fields", "field_key"],
    ["/api/published-blocks", "block_key"],
  ])("keeps %s entity links under the Vite app basename", (endpoint, idKey) => {
    const config = buildGridConfig(endpoint, idKey, [], registryListPath(endpoint), false);

    expect(firstLinkHref(config)).toBe(`/app${registryListPath(endpoint)}/{${idKey}}`);
  });

  it("keeps dataset links under the Vite app basename", () => {
    expect(firstLinkHref(datasetGridConfig())).toBe("/app/registry/datasets/{dataset_id}");
  });
});
