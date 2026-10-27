import { createContext, useContext } from "react";

/** Author step ids that return a kept test output in test runs (builder test data, not structure). */
export const KeptTestOutputStepsContext = createContext<ReadonlySet<string>>(new Set());

export function useStepKeepsTestOutput(nodeId: string, stepId: string | undefined): boolean {
  const kept = useContext(KeptTestOutputStepsContext);
  // A hook member belongs to another workflow; its ids never name the author's kept outputs.
  return !nodeId.startsWith("hook:") && kept.has(stepId || nodeId);
}
