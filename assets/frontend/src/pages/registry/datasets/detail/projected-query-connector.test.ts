import type { DatasetConnector, DatasetRecord } from "../api";
import { projectedQueryConnector } from "./projected-query-connector";

function projectedFixture(install: string, artifact: string, canonicalId: string) {
  const installId = install.trim();
  const connector: DatasetConnector = {
    connector_id: `extension.${installId}.query`,
    local_connector_id: "query",
    source_install_id: installId,
    account_id: "account-1",
    display_name: "Query connector",
    connector_kind: "extension",
    runtime_kind: "local_extension",
    query_language: "json",
  };
  const dataset: DatasetRecord = {
    dataset_id: canonicalId,
    workspace_id: "workspace-1",
    availability_mode: "specific_projects_only",
    display_name: "Projected dataset",
    connector_kind: "extension",
    connector_id: "query",
    runtime_kind: "local_extension",
    query_language: "json",
    read_only: true,
    enabled: true,
    account_id: "account-1",
    source_install_id: installId,
    metadata_jsonb: {
      account_id: "account-1",
      projection_bridge: {
        immutable: true,
        install_id: install,
        artifact_id: artifact,
        release_id: "release-1",
        artifact_kind: "dataset",
      },
    },
  };
  return { dataset, connector };
}

// Canonical handles follow the backend SAFE_ID substitution and strip("-").
it.each([
  ["install", "orders", "extension.install.orders"],
  ["  --Install_9.1--  ", " --Orders_2.0-- ", "extension.Install_9.1.Orders_2.0"],
  ["/install@team/", "{orders}", "extension.install-team.orders"],
  ["a---b", "c--d", "extension.a---b.c--d"],
  ["--._--", "--_.--", "extension.._._."],
  ["é/install", "订单/orders", "extension.install.orders"],
])("preserves the canonical bridge for %s and %s", (install, artifact, canonicalId) => {
  const { dataset, connector } = projectedFixture(install, artifact, canonicalId);
  expect(projectedQueryConnector(dataset, [connector])).toBe(connector);
});

it.each(["", " \t\n", "-", "-----", "/?!", "订单"])(
  "rejects an empty normalized bridge part %j on either side",
  part => {
    const install = projectedFixture(part, "orders", "extension..orders");
    const artifact = projectedFixture("install", part, "extension.install.");
    expect(projectedQueryConnector(install.dataset, [install.connector])).toBeUndefined();
    expect(projectedQueryConnector(artifact.dataset, [artifact.connector])).toBeUndefined();
  },
);

it("keeps exact identity checks after canonical normalization", () => {
  const { dataset, connector } = projectedFixture("--install--", "/orders/", "extension.install.orders");
  dataset.dataset_id = "extension.install.Orders";
  expect(projectedQueryConnector(dataset, [connector])).toBeUndefined();
});

it("trims long edge runs and rejects an all-hyphen part", () => {
  const edge = "-".repeat(100_000);
  const { dataset, connector } = projectedFixture(`${edge}install`, `orders${edge}`, "extension.install.orders");
  expect(projectedQueryConnector(dataset, [connector])).toBe(connector);
  const empty = projectedFixture(edge, "orders", "extension..orders");
  expect(projectedQueryConnector(empty.dataset, [empty.connector])).toBeUndefined();
});

it("handles a long interior hyphen suffix within a generous interactive budget", () => {
  const installId = `install${"-".repeat(100_000)}z`;
  const artifactId = `orders${"-".repeat(100_000)}z`;
  const { dataset, connector } = projectedFixture(installId, artifactId, `extension.${installId}.${artifactId}`);
  const started = performance.now();
  const selected = projectedQueryConnector(dataset, [connector]);
  const elapsed = performance.now() - started;
  expect(selected).toBe(connector);
  // Linear scans take milliseconds; the previous backtracking suffix takes seconds.
  expect(elapsed).toBeLessThan(1_000);
});
