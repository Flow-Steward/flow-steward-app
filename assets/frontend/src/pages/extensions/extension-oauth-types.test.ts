import {
  oauthConnectionTypesFromManifest,
  oauthProvidersFromManifest,
} from "@/pages/extensions/extension-oauth-types";

describe("compiled OAuth manifest projection", () => {
  it("projects only provider-local OAuth declarations without inventing provider policy", () => {
    const manifest = {
      oauth_providers: [
        {
          provider_id: "example",
          display_name: "Example Identity",
          description: "Provider-owned copy",
          setup_page_id: "oauth_setup",
          help_page_id: "oauth_help",
          error_help_anchors: { provider_unavailable: "provider-status" },
          credential_fields: [
            {
              field_id: "application_key",
              value_type: "text",
              required: true,
              secret: false,
              label: "Application key",
              help_text: "Declared help",
              validation: { min_length: 1, max_length: 100 },
            },
          ],
          authorization_intents: [{ intent_id: "connect", user_inputs: [] }],
        },
      ],
      connection_types: [
        {
          connection_type_id: "mailbox",
          display_name: "Mailbox",
          auth: { type: "oauth2", provider_id: "example", scopes: { required: ["mail.read"] } },
        },
        { connection_type_id: "token", auth: { type: "token" } },
      ],
    };

    expect(oauthProvidersFromManifest(manifest)).toEqual(manifest.oauth_providers);
    expect(oauthConnectionTypesFromManifest(manifest)).toEqual([manifest.connection_types[0]]);
  });

  it("fails closed for raw or incomplete declarations", () => {
    expect(oauthProvidersFromManifest({ oauth_providers: [{ provider_id: "partial" }] })).toEqual([]);
    expect(oauthConnectionTypesFromManifest({ connection_types: "contracts/connections.yaml" })).toEqual([]);
  });
});
