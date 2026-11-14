import {
  bindConnectionFieldSecret,
  createProjectConnection,
  saveProjectConnectionSecrets,
  unbindConnectionFieldSecret,
  updateProjectConnection,
} from "@/pages/projects/connections/api";
import {
  connectionPayloadForSave,
  persistPreparedConnection,
  prepareConnectionSave,
} from "./connection-save";

vi.mock("@/pages/projects/connections/api", () => ({
  bindConnectionFieldSecret: vi.fn(),
  unbindConnectionFieldSecret: vi.fn(),
  saveProjectConnectionSecrets: vi.fn(),
  updateProjectConnection: vi.fn(),
  createProjectConnection: vi.fn(),
  startProjectConnectionOAuth: vi.fn(),
  deleteProjectConnection: vi.fn(),
}));

const component = {
  component_id: "shop",
  type: "connection_form",
  data: { connection_type: "shop_api", required_secret_fields: ["api_token"] },
  fields: [
    { path: "display_name", label: "Display name", widget: "text" },
    { path: "api_token", label: "API token", widget: "secret" },
  ],
};

function persist(values: Record<string, unknown>, projectConnections: unknown[] = []) {
  const preparation = prepareConnectionSave({
    component,
    connectionMatchesCurrentInstall: () => true,
    oauthConnectionTypes: [],
    projectConnections,
    values,
  });
  const payload = connectionPayloadForSave({
    component,
    extensionInstallId: "install-1",
    extensionKey: "acme.shop",
    fieldValues: preparation.fieldValues,
    preparation,
  });
  return persistPreparedConnection({
    onAuthorization: vi.fn(),
    onConnectionCreated: vi.fn(),
    onOAuthStart: vi.fn(),
    payload,
    preparation,
    projectId: "p1",
    returnPath: "/extensions/shop",
  });
}

describe("connection save with a stored secret", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(createProjectConnection).mockResolvedValue({ connection_id: "c-new" });
  });

  it("creates a new connection with a chosen stored secret and types nothing", async () => {
    await persist({ display_name: "Main shop", "__stored_secret__:api_token": "s1" });

    expect(saveProjectConnectionSecrets).not.toHaveBeenCalled();
    // The create carries the choice, so its required-secret check sees it and the
    // platform binds it as it creates the connection; nothing is bound afterwards.
    expect(createProjectConnection).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ enabled: false, secret_refs: { refs: {}, catalog: { api_token: "s1" } } }),
    );
    expect(bindConnectionFieldSecret).not.toHaveBeenCalled();
    expect(updateProjectConnection).toHaveBeenCalledWith("p1", "c-new", { enabled: true });
  });

  it("creates a new connection with a typed secret and no catalog choice", async () => {
    await persist({ display_name: "Main shop", api_token: "typed" });

    expect(createProjectConnection).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ secret_refs: { refs: { api_token: "api_token" } } }),
    );
    expect(saveProjectConnectionSecrets).toHaveBeenCalledWith("p1", "c-new", { api_token: "typed" });
  });

  it("binds a stored secret chosen for an existing connection after the update", async () => {
    const existing = { connection_id: "c1", secret_refs: { refs: {} } };
    await persist({ connection_id: "c1", "__stored_secret__:api_token": "s2" }, [existing]);

    expect(updateProjectConnection).toHaveBeenCalledWith(
      "p1",
      "c1",
      expect.objectContaining({ secret_refs: { refs: {} } }),
    );
    expect(bindConnectionFieldSecret).toHaveBeenCalledWith("p1", "c1", "api_token", "s2");
  });

  it("still requires a credential when neither a value nor a stored secret is given", () => {
    expect(() => persist({ display_name: "Main shop" })).toThrow(/required/);
  });

  it("stops using a stored secret before a typed value is saved", async () => {
    const existing = {
      connection_id: "c1",
      secret_refs: { refs: { api_token: "catalog_secret:s1" }, catalog: { api_token: "s1" } },
    };
    await persist({ connection_id: "c1", "__stored_secret__:api_token": "", api_token: "typed" }, [existing]);

    expect(unbindConnectionFieldSecret).toHaveBeenCalledWith("p1", "c1", "api_token");
    expect(saveProjectConnectionSecrets).toHaveBeenCalledWith("p1", "c1", { api_token: "typed" });
    expect(vi.mocked(unbindConnectionFieldSecret).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(saveProjectConnectionSecrets).mock.invocationCallOrder[0],
    );
    expect(bindConnectionFieldSecret).not.toHaveBeenCalled();
  });
});

describe("connection save of fields the form does not show", () => {
  const typedFields = [
    { path: "display_name", label: "Display name", widget: "text" },
    { path: "relay_mode", label: "Relay", widget: "select" },
    { path: "relay_port", label: "Relay port", widget: "integer", visible_if: [{ path: "relay_mode", eq: "relay" }] },
    { path: "retry_minutes", label: "Retry minutes", widget: "number", visible_if: [{ path: "relay_mode", eq: "relay" }] },
    { path: "verify_tls", label: "Verify TLS", widget: "boolean", visible_if: [{ path: "relay_mode", eq: "relay" }] },
    { path: "batch_size", label: "Batch size", widget: "integer" },
  ];
  const configFields = ["relay_mode", "relay_port", "retry_minutes", "verify_tls", "batch_size", "region"];

  function prepare(
    values: Record<string, unknown>,
    { declareConfigFields, existing }: { declareConfigFields: boolean; existing?: Record<string, unknown> },
  ) {
    return prepareConnectionSave({
      component: {
        component_id: "feed",
        type: "connection_form",
        data: { connection_type: "acme_feed_api", ...(declareConfigFields ? { config_fields: configFields } : {}) },
        fields: typedFields,
      },
      connectionMatchesCurrentInstall: () => true,
      oauthConnectionTypes: [],
      projectConnections: existing ? [existing] : [],
      values,
    });
  }

  // What a form in edit mode holds: every input, the empty ones as "".
  const editValues = {
    connection_id: "c1",
    display_name: "Main feed",
    relay_mode: "none",
    relay_port: "",
    retry_minutes: "",
    verify_tls: "",
    batch_size: "",
    region: "",
  };
  const existing = {
    connection_id: "c1",
    connection_config: { relay_mode: "relay", relay_port: 8080, retry_minutes: 10, batch_size: 50, region: "eu" },
  };

  for (const declareConfigFields of [true, false]) {
    const label = declareConfigFields ? "with config_fields" : "without config_fields";

    it(`keeps the stored value of a hidden typed field on edit (${label})`, () => {
      const { config } = prepare(editValues, { declareConfigFields, existing });

      expect(config.relay_mode).toBe("none");
      expect(config.relay_port).toBe(8080);
      expect(config.retry_minutes).toBe(10);
      expect(config).not.toHaveProperty("verify_tls");
      expect(Object.values(config)).not.toContain("");
    });

    it(`writes no hidden typed field on create (${label})`, () => {
      const { config } = prepare(
        { display_name: "Main feed", relay_mode: "none", relay_port: "", retry_minutes: "", verify_tls: "", batch_size: "20", region: "" },
        { declareConfigFields },
      );

      expect(config).toEqual({ relay_mode: "none", batch_size: 20 });
    });

    it(`writes a typed field once it is shown (${label})`, () => {
      const { config } = prepare(
        { ...editValues, relay_mode: "relay", relay_port: "9090", retry_minutes: "5" },
        { declareConfigFields, existing },
      );

      expect(config.relay_port).toBe(9090);
      expect(config.retry_minutes).toBe(5);
    });

    it(`stores no empty string for an emptied visible number field (${label})`, () => {
      const { config } = prepare(editValues, { declareConfigFields, existing });

      expect(config).not.toHaveProperty("batch_size");
    });
  }

  it("keeps a config_fields value that is not a form field unless the form supplies one", () => {
    expect(prepare(editValues, { declareConfigFields: true, existing }).config.region).toBe("eu");
    expect(prepare({ ...editValues, region: "us" }, { declareConfigFields: true, existing }).config.region).toBe("us");
  });
});
