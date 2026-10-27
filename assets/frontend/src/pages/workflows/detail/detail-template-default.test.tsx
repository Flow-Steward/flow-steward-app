import { defaultOverrideForStepKind } from "@/pages/workflows/detail/detail-step-editing";

describe("a new Template step", () => {
  it("starts as plain text, the agreed default", () => {
    const config = JSON.parse(defaultOverrideForStepKind("template")).template;

    expect(config.format).toBe("text");
    expect(config.text).not.toMatch(/^#/);
  });
});
