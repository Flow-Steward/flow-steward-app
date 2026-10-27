import { renderHook } from "@testing-library/react";

import { useRememberProject } from "@/lib/utils/use-remember-project";

describe("extension project selection", () => {
  beforeEach(() => localStorage.clear());

  it("remembers the visited project and follows a project change", () => {
    const { rerender } = renderHook(({ projectId }) => useRememberProject(projectId), {
      initialProps: { projectId: "project-one" },
    });
    expect(localStorage.getItem("platform-last-project-id")).toBe("project-one");

    rerender({ projectId: "project-two" });

    expect(localStorage.getItem("platform-last-project-id")).toBe("project-two");
  });

  it("retains the last project when a page has no project context", () => {
    localStorage.setItem("platform-last-project-id", "project-one");

    renderHook(() => useRememberProject(null));

    expect(localStorage.getItem("platform-last-project-id")).toBe("project-one");
  });
});
