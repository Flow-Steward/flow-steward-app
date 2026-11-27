import { featureState, holdsAnyEntitlement, requiredEntitlements } from "@/lib/auth/features";

type Session = Parameters<typeof featureState>[0];

describe("featureState", () => {
  const endpoint = "/api/modules/acme.approvals/approval-steps";

  it("is enabled only with the flag on and an endpoint named", () => {
    expect(featureState({ feature_flags: { approval_assignees: true }, feature_endpoints: { approval_assignees: endpoint } }, "approval_assignees"))
      .toEqual({ enabled: true, endpoint });
  });

  const disabled: Array<[string, Session]> = [
    ["no session", null],
    ["no flag", { feature_flags: {}, feature_endpoints: { approval_assignees: endpoint } }],
    ["flag off", { feature_flags: { approval_assignees: false }, feature_endpoints: { approval_assignees: endpoint } }],
    ["no endpoints", { feature_flags: { approval_assignees: true } }],
    ["blank endpoint", { feature_flags: { approval_assignees: true }, feature_endpoints: { approval_assignees: "  " } }],
  ];
  it.each(disabled)("is disabled with %s", (_label, session) => {
    expect(featureState(session, "approval_assignees")).toEqual({ enabled: false, endpoint: "" });
  });
});

describe("holdsAnyEntitlement", () => {
  const flags = { "acme.base": true, "acme.extra": false };

  it("needs nothing when the page names no feature", () => {
    expect(holdsAnyEntitlement({}, undefined)).toBe(true);
    expect(holdsAnyEntitlement({}, "")).toBe(true);
    expect(holdsAnyEntitlement({}, [])).toBe(true);
  });

  it("keeps the single-feature form", () => {
    expect(holdsAnyEntitlement(flags, "Acme.Base")).toBe(true);
    expect(holdsAnyEntitlement(flags, "acme.extra")).toBe(false);
    expect(holdsAnyEntitlement(null, "acme.base")).toBe(false);
  });

  it("opens a page for any one feature of a list", () => {
    expect(holdsAnyEntitlement(flags, ["acme.extra", "acme.base"])).toBe(true);
    expect(holdsAnyEntitlement(flags, ["acme.extra", "acme.other"])).toBe(false);
  });

  it("reads names trimmed and in lower case", () => {
    expect(requiredEntitlements([" Acme.Base ", "", 3, "acme.extra"])).toEqual(["acme.base", "acme.extra"]);
  });
});
