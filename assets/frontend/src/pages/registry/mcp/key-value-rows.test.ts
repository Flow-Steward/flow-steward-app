import {
  defaultHeaderSecretPrefix,
  keyValueObject,
  keyValueRowProblem,
  keyValueSecretChoices,
  type KeyValueRow,
  withHeaderSecretChoice,
} from "./key-value-rows";

describe("header rows that send a stored secret", () => {
  it("starts Authorization with a Bearer prefix and other headers with none", () => {
    expect(defaultHeaderSecretPrefix(" authorization ")).toBe("Bearer ");
    expect(defaultHeaderSecretPrefix("X-Api-Key")).toBe("");
  });

  it("clears the typed value when a secret is chosen; the system sets the prefix", () => {
    const typed: KeyValueRow = { id: 1, key: "Authorization", value: "Bearer typed" };
    const chosen = withHeaderSecretChoice(typed, "s1");
    expect(chosen).toMatchObject({ secretId: "s1", value: "", prefix: "Bearer " }); // pragma: allowlist secret - a form key / test id, not a secret

    const apiKey = withHeaderSecretChoice({ id: 2, key: "X-Api-Key", value: "" }, "s2");
    expect(apiKey).toMatchObject({ secretId: "s2", prefix: "" }); // pragma: allowlist secret - a form key / test id, not a secret

    expect(withHeaderSecretChoice(chosen, "").secretId).toBe("");
  });

  it("sends Bearer before a secret only for Authorization, whatever the row held", () => {
    const rows: KeyValueRow[] = [
      { id: 1, key: "Authorization", value: "", secretId: "s1", prefix: "Token " }, // pragma: allowlist secret - a form key / test id, not a secret
      { id: 2, key: "X-Api-Key", value: "", secretId: "s2", prefix: "Bearer " }, // pragma: allowlist secret - a form key / test id, not a secret
    ];
    expect(keyValueSecretChoices(rows, "Header")).toEqual({
      Authorization: { secret_id: "s1", prefix: "Bearer " }, // pragma: allowlist secret - a test id, not a secret
      "X-Api-Key": { secret_id: "s2", prefix: "" }, // pragma: allowlist secret - a test id, not a secret
    });
  });

  it("sends chosen rows as choices and typed rows as headers", () => {
    const rows: KeyValueRow[] = [
      { id: 1, key: "Authorization", value: "", secretId: "s1", prefix: "Bearer " }, // pragma: allowlist secret - a test id, not a secret
      { id: 2, key: "X-Tenant", value: "acme" },
    ];
    expect(keyValueObject(rows, "Header")).toEqual({ "X-Tenant": "acme" });
    expect(keyValueSecretChoices(rows, "Header")).toEqual({
      Authorization: { secret_id: "s1", prefix: "Bearer " }, // pragma: allowlist secret - a test id, not a secret
    });
  });

  it("needs a header name for a chosen secret and a unique name across both kinds", () => {
    const nameless: KeyValueRow[] = [{ id: 1, key: "", value: "", secretId: "s1" }]; // pragma: allowlist secret - a test id, not a secret
    expect(keyValueRowProblem(nameless, 0, "Header")).toBe("Enter the header name for the stored secret.");

    const twice: KeyValueRow[] = [
      { id: 1, key: "Authorization", value: "typed" },
      { id: 2, key: "authorization", value: "", secretId: "s1" }, // pragma: allowlist secret - a test id, not a secret
    ];
    expect(() => keyValueSecretChoices(twice, "Header")).toThrow("Header keys must be unique.");
  });

  it("limits the prefix to 32 characters", () => {
    const rows: KeyValueRow[] = [{ id: 1, key: "Authorization", value: "", secretId: "s1", prefix: "x".repeat(33) }]; // pragma: allowlist secret - a test id, not a secret
    expect(keyValueRowProblem(rows, 0, "Header")).toBe("Header prefixes must be 32 characters or fewer.");
  });
});
