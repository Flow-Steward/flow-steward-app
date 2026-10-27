import {
  clusterAdvancedFields,
  conditionallyRequiredNames,
  groupConnectionFormFields,
  schemaFields,
  mutationMessage,
  type ConnectionSchemaField,
} from "./data-connection-helpers";

const field = (key: string): ConnectionSchemaField => ({
  key,
  label: key,
  description: "",
  enum_values: [],
  required: false,
  type: "string",
});

it("puts standard database settings before advanced settings", () => {
  const groups = groupConnectionFormFields(
    [field("query_params"), field("database"), field("port"), field("host"), field("user")],
    [field("password"), field("certificate")],
  );

  expect(groups.primary.map(({ key, storage }) => [key, storage])).toEqual([
    ["host", "connection"],
    ["port", "connection"],
    ["database", "connection"],
    ["user", "connection"],
    ["password", "secret"],
  ]);
  expect(groups.advanced.map(({ key, storage }) => [key, storage])).toEqual([
    ["query_params", "connection"],
    ["certificate", "secret"],
  ]);
});

it("keeps unknown connector-specific fields in advanced settings", () => {
  expect(
    groupConnectionFormFields([field("project"), field("instance")], []).advanced.map(({ key }) => key),
  ).toEqual(["project", "instance"]);
});

it("keeps a connection-schema password advanced when a secret password is primary", () => {
  const groups = groupConnectionFormFields([field("password")], [field("password")]);

  expect(groups.primary.map(({ key, storage }) => [key, storage])).toEqual([["password", "secret"]]);
  expect(groups.advanced.map(({ key, storage }) => [key, storage])).toEqual([["password", "connection"]]);
});

it("does not expose connection hosts in failed action feedback", () => {
  expect(
    mutationMessage(
      {
        ok: false,
        errors: [
          {
            code: "connection_failed",
            message: "connection refused at 172.18.0.4:5432",
          },
        ],
      },
      { success: "Connected.", failure: "Connection test failed." },
    ),
  ).toBe("Connection test failed.");
});

describe("required-field validation covers both groups", () => {
  // A5-005: create validated only the Advanced group, so a connection could be
  // created with its primary connector fields blank. Both groups expose the
  // same `required` flag from the connector schema, so both must be checked.
  const requiredField = (key: string): ConnectionSchemaField => ({
    ...field(key),
    required: true,
  });

  it("puts required primary fields where a validator can reach them", () => {
    const groups = groupConnectionFormFields(
      [requiredField("host"), requiredField("timeout_seconds")],
      [],
    );

    expect(groups.primary.map(({ key }) => key)).toContain("host");
    expect(groups.advanced.map(({ key }) => key)).toContain("timeout_seconds");
    expect(
      [...groups.primary, ...groups.advanced].filter(({ required }) => required),
    ).toHaveLength(2);
  });

  it("keeps the password on the primary group where the operator sees it", () => {
    const groups = groupConnectionFormFields([], [requiredField("password")]);

    expect(groups.primary.map(({ key }) => key)).toEqual(["password"]);
    expect(groups.primary[0].storage).toBe("secret");
  });
});

describe("requirements follow the chosen source", () => {
  // RS5-014: `prebuilt` was the only required field, so the form enabled Create
  // for a PostgreSQL connection with no host. The schema now declares the
  // requirement conditionally and the form reads the same declaration.
  const schema = {
    type: "object",
    required: ["prebuilt"],
    properties: {
      prebuilt: { type: "string", enum: ["postgres", "sqlite"] },
      host: { type: "string" },
      database: { type: "string" },
      user: { type: "string" },
    },
    allOf: [
      {
        if: { properties: { prebuilt: { enum: ["postgres"] } }, required: ["prebuilt"] },
        then: { required: ["host", "database", "user"] },
      },
      {
        if: { properties: { prebuilt: { const: "sqlite" } }, required: ["prebuilt"] },
        then: { required: ["database"] },
      },
    ],
  } as Record<string, unknown>;

  it("requires a host for a host-addressed source", () => {
    const required = conditionallyRequiredNames(schema, { prebuilt: "postgres" });

    expect([...required].sort()).toEqual(["database", "host", "prebuilt", "user"]);
  });

  it("does not require a host for a file-backed source", () => {
    const required = conditionallyRequiredNames(schema, { prebuilt: "sqlite" });

    expect([...required].sort()).toEqual(["database", "prebuilt"]);
    expect(required.has("host")).toBe(false);
  });

  it("adds nothing while the source is still unchosen", () => {
    expect([...conditionallyRequiredNames(schema, {})]).toEqual(["prebuilt"]);
  });

  it("marks the fields required on the form when the source is preset", () => {
    // The discriminator is preset by the catalog variant and omitted from the
    // form, so it reaches requiredness through `values`, not through an input.
    const fields = schemaFields(schema, { omit: ["prebuilt"], values: { prebuilt: "postgres" } });
    const required = fields.filter((field) => field.required).map((field) => field.key);

    expect(required.sort()).toEqual(["database", "host", "user"]);
    expect(fields.some((field) => field.key === "prebuilt")).toBe(false);
  });
});

describe("advanced fields cluster by what the operator decides together", () => {
  const advanced = (keys: string[]) =>
    keys.map((key) => ({ ...field(key), storage: "connection" as const }));

  it("uses the published clusters and their order", () => {
    const clusters = clusterAdvancedFields(
      advanced([
        "warehouse",
        "env",
        "ip_type",
        "region",
        "role",
        "dialect",
        "protocol",
        "project",
        "schema",
        "instance",
        "query_params",
        "account",
        "cluster",
      ]),
    );

    expect(clusters.map((cluster) => cluster.legend)).toEqual([
      "Runtime environment",
      "Cloud placement",
      "Transport",
      "SQL target",
    ]);
    expect(clusters[0].fields.map((item) => item.key)).toEqual(["env", "role"]);
    expect(clusters[1].fields.map((item) => item.key)).toEqual([
      "project",
      "region",
      "instance",
      "cluster",
    ]);
    expect(clusters[2].fields.map((item) => item.key)).toEqual([
      "protocol",
      "ip_type",
      "query_params",
    ]);
    expect(clusters[3].fields.map((item) => item.key)).toEqual([
      "dialect",
      "schema",
      "account",
      "warehouse",
    ]);
  });

  it("collects fields no published cluster names", () => {
    const clusters = clusterAdvancedFields(advanced(["role", "acme_tuning", "acme_mode"]));

    expect(clusters.map((cluster) => cluster.legend)).toEqual([
      "Runtime environment",
      "Other settings",
    ]);
    expect(clusters[1].fields.map((item) => item.key)).toEqual([
      "acme_tuning",
      "acme_mode",
    ]);
  });

  it("leaves a short unknown set as one unlabelled group", () => {
    // Canon clusters "past about five fields"; below that a legend would name
    // a group the operator never has to navigate.
    const clusters = clusterAdvancedFields(advanced(["acme_one", "acme_two"]));

    expect(clusters).toHaveLength(1);
    expect(clusters[0].legend).toBe("");
    expect(clusters[0].fields.map((item) => item.key)).toEqual(["acme_one", "acme_two"]);
  });

  it("returns nothing for no advanced fields", () => {
    expect(clusterAdvancedFields([])).toEqual([]);
  });
});

describe("a branch can narrow a field to a fixed set", () => {
  // Toolbox declares these per source, not per connector: `dialect` only means
  // something for Spanner, `protocol` only for ClickHouse. The schema says so
  // the same way it declares conditional requirements — allOf + if/then keyed
  // on the discriminator.
  const schema = {
    type: "object",
    properties: {
      prebuilt: { type: "string", title: "Toolbox source", enum: ["spanner", "clickhouse"] },
      dialect: { type: "string", title: "SQL dialect" },
      protocol: { type: "string", title: "Protocol" },
    },
    allOf: [
      {
        if: { properties: { prebuilt: { const: "spanner" } } },
        then: { properties: { dialect: { enum: ["googlesql", "postgresql"] } } },
      },
      {
        if: { properties: { prebuilt: { const: "clickhouse" } } },
        then: { properties: { protocol: { enum: ["native", "http"] } } },
      },
    ],
  };

  const fieldFor = (key: string, values: Record<string, string>) =>
    schemaFields(schema, { values }).find((item) => item.key === key);

  it("offers the branch's values once its condition holds", () => {
    expect(fieldFor("dialect", { prebuilt: "spanner" })?.enum_values).toEqual([
      "googlesql",
      "postgresql",
    ]);
    expect(fieldFor("protocol", { prebuilt: "clickhouse" })?.enum_values).toEqual([
      "native",
      "http",
    ]);
  });

  it("leaves the field free text while another source is chosen", () => {
    expect(fieldFor("dialect", { prebuilt: "clickhouse" })?.enum_values).toEqual([]);
    expect(fieldFor("protocol", { prebuilt: "spanner" })?.enum_values).toEqual([]);
  });

  it("keeps an enum declared on the property itself", () => {
    expect(fieldFor("prebuilt", {})?.enum_values).toEqual(["spanner", "clickhouse"]);
  });
});
