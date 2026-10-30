import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Node, ReactFlowInstance } from "@xyflow/react";

import { ignoreAsyncResult } from "@/lib/utils/async";
import { absoluteNodeCenter } from "./canvas-core";
import { runtimeMarkState } from "./runtime-mark-state";
import { canvasMotionDuration } from "./canvas-viewport";
import type { HookExecutionMark } from "@/pages/workflows/detail/detail-hook-execution-marks";

type Options = {
  instance: ReactFlowInstance | null;
  nodes: readonly Node[];
  /** The selected run's identity; a new run follows again. */
  runKey: string;
  /** Only an unfinished run is followed. */
  active: boolean;
};

/**
 * The canvas follows the running step until the author looks somewhere
 * themselves (moves or zooms the view, opens a step); then it stays, and
 * "Follow run" brings the running step back to the middle and follows again.
 */
export function useCanvasRunFollow({ instance, nodes, runKey, active }: Options) {
  const [pausedRun, setPausedRun] = useState("");
  const paused = Boolean(runKey) && pausedRun === runKey;
  const nodesById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  // Where the run is: the step that runs, or the one that waits (an Approval, a Wait).
  const runningId = useMemo(() => {
    const state = (node: Node) => runtimeMarkState((node.data as { runtimeMark?: HookExecutionMark } | undefined)?.runtimeMark);
    return (nodes.find((node) => state(node) === "running") ?? nodes.find((node) => state(node) === "waiting"))?.id ?? "";
  }, [nodes]);
  const centred = useRef("");
  const centre = useCallback((nodeId: string) => {
    const node = nodesById.get(nodeId);
    if (!instance || !node) return;
    const point = absoluteNodeCenter(node, nodesById);
    ignoreAsyncResult(instance.setCenter(point.x, point.y, { duration: canvasMotionDuration(), zoom: instance.getZoom() }));
  }, [instance, nodesById]);
  useEffect(() => {
    if (!active || paused || !runningId) return;
    const key = `${runKey}:${runningId}`;
    if (centred.current === key) return;
    centred.current = key;
    centre(runningId);
  }, [active, centre, paused, runKey, runningId]);
  /** The author moved the view (only user gestures carry an event). */
  const pause = useCallback(() => {
    if (active && runKey) setPausedRun(runKey);
  }, [active, runKey]);
  const resume = useCallback(() => {
    setPausedRun("");
    centred.current = "";
    if (runningId) {
      centred.current = `${runKey}:${runningId}`;
      centre(runningId);
    }
  }, [centre, runKey, runningId]);
  return { paused: paused && active, pause, resume };
}
