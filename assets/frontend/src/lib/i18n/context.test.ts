import { fallbackContext } from "@/lib/i18n/context";

describe("English interface fallback", () => {
  it("interpolates operator values when the bootstrap has not loaded", () => {
    expect(fallbackContext.t("clone.heading", "Clone {name}", { name: "Customer ledger" }))
      .toBe("Clone Customer ledger");
    expect(fallbackContext.t("count.label", "{count} selected", { count: 0 }))
      .toBe("0 selected");
  });

  it("preserves missing placeholders and does not recursively interpret operator values", () => {
    expect(fallbackContext.t("demo", "{name}: {missing}", { name: "{count}", count: 4 }))
      .toBe("{count}: {missing}");
  });
});
