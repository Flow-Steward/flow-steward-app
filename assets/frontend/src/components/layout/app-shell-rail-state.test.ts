import {
  SIDEBAR_RAIL_STORAGE_KEY,
  defaultSidebarRailState,
  readSidebarRailState,
  toggleSidebarRailState,
  writeSidebarRailState,
} from "@/components/layout/app-shell-rail-state";

describe("app shell rail state", () => {
  it.each([
    [768, "collapsed"],
    [1023, "collapsed"],
    [1024, "expanded"],
    [1440, "expanded"],
  ] as const)("uses the breakpoint default at %i px", (width, expected) => {
    expect(defaultSidebarRailState(width)).toBe(expected);
  });

  it("prefers a stored user choice and rejects corrupted storage", () => {
    localStorage.setItem(SIDEBAR_RAIL_STORAGE_KEY, "collapsed");
    expect(readSidebarRailState(localStorage, 1440)).toBe("collapsed");

    localStorage.setItem(SIDEBAR_RAIL_STORAGE_KEY, "wide");
    expect(readSidebarRailState(localStorage, 1440)).toBe("expanded");
  });

  it("writes and toggles the only two valid states", () => {
    writeSidebarRailState(localStorage, "expanded");
    expect(localStorage.getItem(SIDEBAR_RAIL_STORAGE_KEY)).toBe("expanded");
    expect(toggleSidebarRailState("expanded")).toBe("collapsed");
    expect(toggleSidebarRailState("collapsed")).toBe("expanded");
  });
});
