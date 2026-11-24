import { HttpError } from "@/lib/http/errors";
import { fallbackContext } from "@/lib/i18n/context";
import { entityCreateErrorMessage } from "@/lib/i18n/entity-create-error-message";

const hint = "If you don’t see it in the list, check whether it’s in Trash.";

function message(detail: unknown, status = 409) {
  const error = new HttpError({ status, url: "/api/agents", payload: { detail } });
  return entityCreateErrorMessage(error, "Failed to create.", fallbackContext.t);
}

it("adds the Trash hint for a reserved ID without a visible registry match", () => {
  expect(message({
    code: "registry_id_conflict",
    message: "Agent ID 'summary' is already taken. Choose another ID.",
    matches: [],
  })).toContain(hint);
});

it("keeps the original message when the conflicting entity is visible", () => {
  const text = "Agent ID 'summary' is already taken.";
  expect(message({
    code: "registry_id_conflict",
    message: text,
    matches: [{ id: "summary" }],
  })).toBe(text);
});

it("leaves validation and unrelated conflicts alone", () => {
  expect(message("Name already exists", 422)).toBe("Name already exists");
  expect(message("The workspace changed")).toBe("The workspace changed");
});
