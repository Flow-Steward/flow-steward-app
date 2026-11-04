import {
  bindConnectionFieldSecret,
  createProjectConnection,
  saveProjectConnectionSecrets,
  unbindConnectionFieldSecret,
  updateProjectConnection,
} from "@/pages/projects/connections/api";
import { persistPreparedConnection, prepareConnectionSave } from "./connection-save";

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
  return persistPreparedConnection({
    onAuthorization: vi.fn(),
    onConnectionCreated: vi.fn(),
    onOAuthStart: vi.fn(),
    payload: { display_name: "Main shop" },
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
    expect(bindConnectionFieldSecret).toHaveBeenCalledWith("p1", "c-new", "api_token", "s1");
    // Bound before the new connection is enabled.
    expect(vi.mocked(bindConnectionFieldSecret).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(updateProjectConnection).mock.invocationCallOrder[0],
    );
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
