import { act, renderHook } from "@testing-library/react";
import { useWorkflowHookNavigation } from "./detail-hook-navigation";
import { hookMemberId } from "@/pages/workflows/builder/hook-frames";

type Options = Parameters<typeof useWorkflowHookNavigation>[0];
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
function navigation(confirmDiscardParameters = vi.fn(async () => true)) {
  const activateHook = vi.fn(() => true);
  const setSearchParams = vi.fn();
  const hookScope = { authorWorkflowId: "author", frames: new Map(), effectiveHookId: "", framesByHook: new Map(),
    childChangeSet: { hasChanges: false, status: "idle" }, childRows: { definitionRows: { groupRows: [], stepRowsById: new Map() } },
    canActivateHook: () => true, activateHook } as unknown as Options["hookScope"];
  const view = renderHook(() => useWorkflowHookNavigation({ hookScope, ownerKey: "author:alpha:7", authorGroupRows: [], authorStructureLocked: false,
    drawerMode: "", selectedGroupId: "", selectedStepId: "", confirmDiscardParameters,
    searchParams: new URLSearchParams(), setSearchParams, setSaveError: vi.fn() }));
  return { ...view, activateHook, setSearchParams };
}

it("composes a read-only request owner with the actual Hook navigation drawer guard", async () => {
  const guard = deferred<boolean>();
  let current = true;
  const openStepDrawer = vi.fn(async (_step: string, _block: string, _skip: boolean, _tab: string, isCurrent: () => boolean) =>
    await guard.promise && isCurrent());
  const hook = navigation();
  try {
    const drawer = { openStepDrawer } as unknown as Parameters<typeof hook.result.current.canvasNavigation>[0];
    let opening!: Promise<boolean | void>;
    act(() => { opening = hook.result.current.canvasNavigation(drawer).openStep("transform", "", "smart-setup", () => current); });
    expect(openStepDrawer).toHaveBeenCalledOnce();
    current = false;
    await act(async () => { guard.resolve(true); expect(await opening).toBe(false); });
    expect(hook.setSearchParams).not.toHaveBeenCalled();
    expect(hook.activateHook).not.toHaveBeenCalled();
  } finally { hook.unmount(); }
});

it("does not activate or route a Hook after an external request retires during parameter confirmation", async () => {
  const confirmation = deferred<boolean>();
  const hook = navigation(vi.fn(() => confirmation.promise));
  let current = true;
  try {
    const drawer = { openStepDrawer: vi.fn() } as unknown as Parameters<typeof hook.result.current.canvasNavigation>[0];
    let opening!: Promise<boolean | void>;
    act(() => { opening = hook.result.current.canvasNavigation(drawer).openStep(hookMemberId("before_finish", "transform"), "", "smart-setup", () => current); });
    current = false;
    await act(async () => { confirmation.resolve(true); expect(await opening).toBe(false); });
    expect(hook.activateHook).not.toHaveBeenCalled();
    expect(hook.setSearchParams).not.toHaveBeenCalled();
  } finally { hook.unmount(); }
});

it("preserves the normal openStep caller without an external predicate", async () => {
  const hook = navigation();
  const openStepDrawer = vi.fn(async () => true);
  try {
    const drawer = { openStepDrawer } as unknown as Parameters<typeof hook.result.current.canvasNavigation>[0];
    await act(async () => { expect(await hook.result.current.canvasNavigation(drawer).openStep("transform")).toBe(true); });
    expect(openStepDrawer).toHaveBeenCalledOnce();
  } finally { hook.unmount(); }
});
