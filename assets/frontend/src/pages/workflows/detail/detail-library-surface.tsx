/* eslint-disable @typescript-eslint/no-explicit-any */
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export function WorkflowStepLibrarySurface({
  ctx,
}: Readonly<{
  ctx: Record<string, any>;
}>) {
  const drawerMode = firstStringFromUnknownValue([ctx.drawerMode, ""]);
  const openNewStepDrawer = ctx.openNewStepDrawer as (
    context: { groupId?: string; laneId?: string },
    blockKey: string,
  ) => void;
  const publishedBlocks = ctx.publishedBlocks as Array<Record<string, unknown>>;
  const selectedGroupId = firstStringFromUnknownValue([ctx.selectedGroupId, ""]);
  const setStepBlockKey = ctx.setStepBlockKey as (value: string) => void;

  return (
    <div
      className={drawerMode === "library" ? "mt-3 block" : "mt-3 hidden"}
      id="library-cards"
    >
      {publishedBlocks.map((block) => (
        <button
          className="mb-2 w-full rounded-xl border border-default px-3 py-2 text-left text-sm fs-ink-primary"
          key={firstStringFromUnknownValue([block.block_key, ""])}
          onClick={() => {
            const blockKey = firstStringFromUnknownValue([block.block_key, ""]).trim();
            setStepBlockKey(blockKey);
            // The new step goes where the author is working: the selected group, or the root.
            openNewStepDrawer({ groupId: selectedGroupId }, blockKey);
          }}
          type="button"
        >
          {firstStringFromUnknownValue([block.display_name, block.block_key])}
        </button>
      ))}
    </div>
  );
}
