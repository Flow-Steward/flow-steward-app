import { fetchJson } from "@/lib/http/client";
import {
  deleteExtensionOAuthConfiguration,
  disconnectExtensionOAuthConnection,
  ensureExtensionOAuthRedirectUri,
  fetchExtensionOAuthConfigurations,
  fetchExtensionOAuthSetupStatus,
  previewExtensionOAuthConfiguration,
  previewExtensionOAuthConfigurationDelete,
  retryExtensionOAuthPostConnect,
  saveExtensionOAuthConfiguration,
  startExtensionOAuthAuthorization,
} from "@/pages/extensions/extension-oauth-api";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedFetchJson = vi.mocked(fetchJson);

describe("extension-owned OAuth API", () => {
  beforeEach(() => mockedFetchJson.mockReset());

  it("uses only the exact project/install/provider-scoped endpoints", async () => {
    mockedFetchJson.mockResolvedValue({});
    const scope = { projectId: "project 1", installId: "install/1", providerId: "identity" };
    const mutation = {
      configuration_id: "11111111-1111-4111-8111-111111111111",
      display_name: "Primary",
      values: { client_id: "client" },
      secret_replacements: { client_secret: "secret" },
      expected_revision: 3,
    };

    await fetchExtensionOAuthConfigurations(scope);
    await fetchExtensionOAuthSetupStatus(scope);
    await ensureExtensionOAuthRedirectUri(scope);
    await previewExtensionOAuthConfiguration(scope, mutation);
    await saveExtensionOAuthConfiguration(scope, { ...mutation, confirmation_token: "confirm" });
    await startExtensionOAuthAuthorization(scope, {
      configuration_id: mutation.configuration_id,
      connection_type: "mailbox",
      intent_id: "connect",
      display_name: "Mailbox",
      return_page_id: "connections",
      intent_inputs: { email_hint: "person@example.test" },
    });
    await retryExtensionOAuthPostConnect(scope, "connection 1", {
      configuration_id: mutation.configuration_id,
      connection_type: "mailbox",
    });
    await disconnectExtensionOAuthConnection(scope, mutation.configuration_id, "connection 1");
    await previewExtensionOAuthConfigurationDelete(scope, mutation.configuration_id, 3);
    await deleteExtensionOAuthConfiguration(scope, mutation.configuration_id, 3, "delete-confirm");

    const prefix = "/api/projects/project%201/extensions/install%2F1/oauth/identity";
    expect(mockedFetchJson.mock.calls.map(([url]) => url)).toEqual([
      `${prefix}/configurations`,
      `${prefix}/setup-status`,
      `${prefix}/callback-registration`,
      `${prefix}/configurations/impact`,
      `${prefix}/configurations`,
      `${prefix}/authorization`,
      `${prefix}/connections/connection%201/post-connect/retry`,
      `${prefix}/configurations/${mutation.configuration_id}/connections/connection%201/disconnect`,
      `${prefix}/configurations/${mutation.configuration_id}/delete-impact`,
      `${prefix}/configurations/${mutation.configuration_id}`,
    ]);
    expect(mockedFetchJson).toHaveBeenNthCalledWith(
      5,
      `${prefix}/configurations`,
      { method: "PUT", body: JSON.stringify({ ...mutation, confirmation_token: "confirm" }) },
    );
  });
});
