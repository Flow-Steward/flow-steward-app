async function loadBootstrap() {
  vi.resetModules();
  await import("@/theme-bootstrap");
}

describe("public shell theme bootstrap", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-platform-theme");
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.classList.remove("dark", "platform-light");
    document.documentElement.style.removeProperty("color-scheme");
  });

  it("restores the primary dark preference before the app renders", async () => {
    localStorage.setItem("platform.ui_theme", "dark");

    await loadBootstrap();

    expect(document.documentElement).toHaveAttribute("data-platform-theme", "dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement).toHaveClass("dark");
    expect(document.documentElement).not.toHaveClass("platform-light");
    expect(document.documentElement).toHaveStyle({ colorScheme: "dark" });
  });

  it("ignores an old-only preference and leaves the root default intact", async () => {
    localStorage.setItem("platform-theme", "light");

    await loadBootstrap();

    expect(document.documentElement).not.toHaveAttribute("data-platform-theme");
    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(document.documentElement).not.toHaveClass("platform-light");
    expect(document.documentElement).not.toHaveClass("dark");
    expect(document.documentElement).toHaveStyle({colorScheme:""});
  });

  it("ignores unsupported stored values", async () => {
    localStorage.setItem("platform.ui_theme", "system");

    await loadBootstrap();

    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(document.documentElement).not.toHaveStyle({ colorScheme: "dark" });
    expect(document.documentElement).not.toHaveStyle({ colorScheme: "light" });
  });

  it("leaves the root default intact when storage is unavailable", async () => {
    const read = vi.spyOn(localStorage, "getItem").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });

    try {
      await expect(loadBootstrap()).resolves.toBeUndefined();
      expect(document.documentElement).not.toHaveAttribute("data-theme");
      expect(document.documentElement).toHaveStyle({colorScheme:""});
    } finally {
      read.mockRestore();
    }
  });
});
