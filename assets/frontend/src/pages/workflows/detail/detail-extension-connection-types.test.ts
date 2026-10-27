import { describe, expect, it } from "vitest";

import {
  extensionConnectionEligibility,
  operationConnectionTypeIds,
} from "@/pages/workflows/detail/detail-extension-connection";

/**
 * An operation can accept more than one connection type. The controller read
 * only the singular `connection_type_id`, so a project holding valid FTP and
 * SFTP connections showed only the FTP one — the SFTP connection saved fine
 * through the API, which is how it was shown to be a display defect and not a
 * policy one.
 */
describe("operation connection types", () => {
  it("reads every declared type", () => {
    expect(
      operationConnectionTypeIds({ connection_type_ids: ["ftp", "sftp"] }),
    ).toEqual(["ftp", "sftp"]);
  });

  it("does not interpret a forbidden singular policy alias", () => {
    expect(operationConnectionTypeIds({ connection_type_id: "custom_smtp" })).toEqual([]);
  });

  it("reads nothing when the operation declares no policy", () => {
    expect(operationConnectionTypeIds({})).toEqual([]);
  });

  it("reads the plural declaration even when malformed legacy data is present", () => {
    expect(
      operationConnectionTypeIds({
        connection_type_ids: ["ftp", "sftp"],
        connection_type_id: "ftp",
      }),
    ).toEqual(["ftp", "sftp"]);
  });
});

describe("eligibility across several accepted types", () => {
  const requirements = { connectionTypeIds: ["ftp", "sftp"], requiredScopes: [] };

  it.each(["ftp", "sftp"])("accepts a %s connection", (connectionType) => {
    expect(
      extensionConnectionEligibility(
        { connection_type: connectionType, enabled: true },
        requirements,
      ),
    ).toMatchObject({ allowed: true, applicable: true });
  });

  it("rejects a type the operation does not declare", () => {
    expect(
      extensionConnectionEligibility(
        { connection_type: "acme_cms_site", enabled: true },
        requirements,
      ),
    ).toMatchObject({ allowed: false, optionSuffix: "Incompatible" });
  });

  it("applies no constraint when the operation declares none", () => {
    expect(
      extensionConnectionEligibility(
        { connection_type: "anything", enabled: true },
        { connectionTypeIds: [], requiredScopes: [] },
      ),
    ).toMatchObject({ allowed: true, applicable: false });
  });

  it.each(["unknown", "degraded"])(
    "allows a connection with %s health while runtime is available",
    (healthStatus) => {
      expect(
        extensionConnectionEligibility(
          {
            connection_type: "ftp",
            enabled: true,
            health_status: healthStatus,
            runtime_status: "available",
          },
          requirements,
        ),
      ).toMatchObject({ allowed: true, applicable: true });
    },
  );

  it.each(["error", "failed", "unavailable"])(
    "rejects a connection with %s health",
    (healthStatus) => {
      expect(
        extensionConnectionEligibility(
          {
            connection_type: "ftp",
            enabled: true,
            health_status: healthStatus,
            runtime_status: "available",
          },
          requirements,
        ),
      ).toMatchObject({ allowed: false, optionSuffix: "Needs attention" });
    },
  );

  it.each(["failed", "incompatible", "unavailable"])(
    "rejects a connection with %s runtime",
    (runtimeStatus) => {
      expect(
        extensionConnectionEligibility(
          {
            connection_type: "ftp",
            enabled: true,
            health_status: "unknown",
            runtime_status: runtimeStatus,
          },
          requirements,
        ),
      ).toMatchObject({ allowed: false, optionSuffix: "Needs attention" });
    },
  );
});
