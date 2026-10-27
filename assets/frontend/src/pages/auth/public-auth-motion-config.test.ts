import {
  AUTH_FEEDBACK_MOTION,
  AUTH_NOTICE_MOTION,
} from "@/pages/auth/public-auth-motion-config";

describe("public auth feedback motion", () => {
  it("keeps request notices on a stable vertical axis", () => {
    expect(AUTH_NOTICE_MOTION.initial).toEqual({ opacity: 0 });
    expect(AUTH_NOTICE_MOTION.animate).toEqual({ opacity: 1 });
    expect(AUTH_NOTICE_MOTION.exit).toEqual({ opacity: 0 });
  });

  it("retains directional motion for local field feedback", () => {
    expect(AUTH_FEEDBACK_MOTION.initial.transform).toBe("translateY(-4px)");
    expect(AUTH_FEEDBACK_MOTION.animate.transform).toBe("translateY(0px)");
  });
});
