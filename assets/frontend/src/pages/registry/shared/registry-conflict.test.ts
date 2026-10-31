import { describe, expect, it } from "vitest";

import { HttpError } from "@/lib/http/errors";
import { registryConflictFromError } from "@/pages/registry/shared/registry-conflict";

function conflictError(detail: unknown, status = 409) {
  return new HttpError({ status, url: "/api/agents", payload: { detail } });
}

describe("registryConflictFromError", () => {
  it("reads the matches and suggestions of a taken name", () => {
    const conflict = registryConflictFromError(
      conflictError({
        code: "registry_name_conflict",
        message: "Agent name 'Writer' is already used. Choose another name.",
        matches: [
          {
            id: "writer",
            display_name: "Writer",
            enabled: true,
            used_by: [{ entity_type: "workflow", id: "digest", display_name: "Digest" }],
          },
        ],
        suggested_display_name: "Writer 2",
      }),
    );

    expect(conflict).toEqual({
      code: "registry_name_conflict",
      matches: [
        {
          id: "writer",
          display_name: "Writer",
          enabled: true,
          usedBy: [{ entity_type: "workflow", id: "digest", display_name: "Digest" }],
        },
      ],
      suggestedDisplayName: "Writer 2",
      suggestedId: "",
    });
  });

  it("ignores every other failure", () => {
    expect(registryConflictFromError(new Error("offline"))).toBeNull();
    expect(registryConflictFromError(conflictError({ code: "workflow_revision_conflict" }))).toBeNull();
    expect(registryConflictFromError(conflictError("Workflow 'x' already exists"))).toBeNull();
    expect(
      registryConflictFromError(conflictError({ code: "registry_id_conflict", matches: [] }, 400)),
    ).toBeNull();
  });
});
