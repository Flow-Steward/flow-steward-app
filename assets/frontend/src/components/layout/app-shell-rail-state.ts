export type SidebarRailState = "expanded" | "collapsed";

export const SIDEBAR_RAIL_STORAGE_KEY = "fsb-rail";

export function isSidebarRailState(value: unknown): value is SidebarRailState {
  return value === "expanded" || value === "collapsed";
}

export function defaultSidebarRailState(viewportWidth: number): SidebarRailState {
  return viewportWidth >= 1024 ? "expanded" : "collapsed";
}

export function readSidebarRailState(
  storage: Pick<Storage, "getItem"> | null,
  viewportWidth: number,
): SidebarRailState {
  const stored = storage?.getItem(SIDEBAR_RAIL_STORAGE_KEY);
  return isSidebarRailState(stored)
    ? stored
    : defaultSidebarRailState(viewportWidth);
}

export function writeSidebarRailState(
  storage: Pick<Storage, "setItem"> | null,
  state: SidebarRailState,
) {
  storage?.setItem(SIDEBAR_RAIL_STORAGE_KEY, state);
}

export function toggleSidebarRailState(state: SidebarRailState): SidebarRailState {
  return state === "expanded" ? "collapsed" : "expanded";
}
