import {
  pageActionTarget,
  pageConnectionCandidates,
  pageConnectionTypes,
  pageUrlParameters,
  queryConnectionTarget,
  queryConnectionTypes,
  selectedPageConnectionId,
  withoutPageUrlParameters,
} from "./extension-page-connection";

const manifest = {
  query_definitions: [
    { query_id: "runs.list", connection: { connection_type_ids: ["service_profile"] } },
    { query_id: "about" },
  ],
};

const rows = [
  { connection_id: "conn-1", connection_type: "service_profile", extension_install_id: "install-1", display_name: "Production" },
  { connection_id: "conn-2", connection_type: "service_profile", extension_install_id: "install-1", display_name: "Staging" },
  { connection_id: "conn-3", connection_type: "service_profile", extension_install_id: "install-2" },
  { connection_id: "conn-4", connection_type: "other_type", extension_install_id: "install-1" },
  { connection_id: "conn-5", connection_type: "service_profile", extension_install_id: "install-1", enabled: false },
];

describe("page connection", () => {
  it("reads the page and query declarations", () => {
    expect(pageConnectionTypes({ connection: { connection_type_ids: ["service_profile"] } })).toEqual(["service_profile"]);
    expect(pageConnectionTypes({ page_id: "about" })).toEqual([]);
    expect(queryConnectionTypes(manifest, "runs.list")).toEqual(["service_profile"]);
    expect(queryConnectionTypes({ compiled_manifest: manifest }, "runs.list")).toEqual(["service_profile"]);
    expect(queryConnectionTypes(manifest, "about")).toEqual([]);
  });

  it("offers only enabled connections of this install and the page types", () => {
    expect(pageConnectionCandidates(rows, ["service_profile"], "install-1").map((row) => row.connection_id))
      .toEqual(["conn-1", "conn-2"]);
    expect(pageConnectionCandidates(rows, [], "install-1")).toEqual([]);
  });

  it("selects the URL connection when it is a candidate, or the only candidate", () => {
    const candidates = pageConnectionCandidates(rows, ["service_profile"], "install-1");
    expect(selectedPageConnectionId(candidates, "conn-2")).toBe("conn-2");
    expect(selectedPageConnectionId(candidates, "conn-3")).toBe("");
    expect(selectedPageConnectionId(candidates, "")).toBe("");
    expect(selectedPageConnectionId(candidates.slice(0, 1), "")).toBe("conn-1");
    expect(selectedPageConnectionId(candidates.slice(0, 1), "conn-9")).toBe("conn-1");
  });

  it("sends a target only to a query that declares a connection", () => {
    expect(queryConnectionTarget(manifest, "runs.list", "conn-1")).toEqual({ target: { connection_id: "conn-1" } });
    expect(queryConnectionTarget(manifest, "about", "conn-1")).toEqual({});
    expect(queryConnectionTarget(manifest, "runs.list", "")).toEqual({});
  });

  it("lets the page connection win over a row value of a row action", () => {
    const target = pageActionTarget({ run_id: "r-1", connection_id: "conn-evil", scope: "row" }, "conn-1", true);
    expect(target).toEqual({ run_id: "r-1", connection_id: "conn-1", scope: "row" });
  });

  it("keeps a connection form's own selection and fills an empty target", () => {
    expect(pageActionTarget({ connection_id: "conn-2" }, "conn-1", false)).toEqual({ connection_id: "conn-2" });
    expect(pageActionTarget({}, "conn-1", false)).toEqual({ connection_id: "conn-1" });
    expect(pageActionTarget({ scope: "row" }, "", true)).toEqual({ scope: "row" });
  });
});

describe("URL page parameters", () => {
  it("reads p.<name> keys with lowercase identifier names and bounded values", () => {
    const long = "x".repeat(1025);
    expect(pageUrlParameters(`?extension_page=run_detail&p.run_id=42&p.Bad=1&p.1x=2&p.note=${long}&project_id=p1`))
      .toEqual({ run_id: "42" });
  });

  it("keeps at most twenty parameters", () => {
    const search = Array.from({ length: 25 }, (_, index) => `p.k${index}=${index}`).join("&");
    expect(Object.keys(pageUrlParameters(search))).toHaveLength(20);
  });

  it("drops a page's parameters and keeps the rest of the URL", () => {
    const params = withoutPageUrlParameters(new URLSearchParams("project_id=p1&p.run_id=42&extension_connection=c1"));
    expect(params.toString()).toBe("project_id=p1&extension_connection=c1");
  });
});
