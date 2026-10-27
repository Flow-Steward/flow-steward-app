import { extensionConnectionEligibility } from "@/pages/workflows/detail/detail-extension-connection";

describe("extension connection eligibility", () => {
  const requirements = {
    connectionTypeIds: ["fixture_account"],
    requiredScopes: ["scope:write"],
  };

  it("accepts a healthy connection with the action's exact required scopes", () => {
    expect(extensionConnectionEligibility({
      connection_type: "fixture_account",
      enabled: true,
      health_status: "healthy",
      runtime_status: "ready",
      oauth_granted_scopes: ["scope:read", "scope:write"],
      connection_config: { oauth: { lifecycle_status: "connected" } },
    }, requirements)).toEqual({
      allowed: true,
      applicable: true,
      guidance: "",
      optionSuffix: "",
    });
  });

  it("blocks missing scopes without knowing the provider or operation id", () => {
    expect(extensionConnectionEligibility({
      connection_type: "fixture_account",
      enabled: true,
      oauth_granted_scopes: ["scope:read"],
      connection_config: { oauth: { lifecycle_status: "connected" } },
    }, requirements)).toMatchObject({
      allowed: false,
      applicable: true,
      optionSuffix: "Additional access required",
    });
  });
});
