function bootstrapStoredTheme(target: Window & typeof globalThis) {
  try {
    const storedTheme = target.localStorage.getItem("platform.ui_theme");
    if (storedTheme !== "light" && storedTheme !== "dark") return;

    const root = target.document.documentElement;
    root.dataset.platformTheme = storedTheme;
    root.dataset.theme = storedTheme;
    root.classList.toggle("dark", storedTheme === "dark");
    root.classList.toggle("platform-light", storedTheme === "light");
    root.style.colorScheme = storedTheme;
  } catch {
    // Storage can be unavailable in hardened or private browser contexts.
  }
}

bootstrapStoredTheme(globalThis as Window & typeof globalThis);

export {};
