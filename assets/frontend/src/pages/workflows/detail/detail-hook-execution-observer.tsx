import { useEffect } from "react";
import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { useJobExecutionSnapshot } from "@/pages/jobs/detail/job-execution-snapshot";
import type { HookExecutionOwner } from "./detail-hook-execution-marks";

export type ExecutionSource = { key: string; jobId: string; frame: HookExecutionOwner; projectId?: string };
export type SnapshotListener = (key: string, snapshot: JobExecutionSnapshot | null) => void;

export function HookExecutionObserver({ source, onSnapshot }: Readonly<{ source: ExecutionSource; onSnapshot: SnapshotListener }>) {
  const args: [string, boolean, string?] = source.projectId ? [source.jobId, true, source.projectId] : [source.jobId, true];
  const { snapshot } = useJobExecutionSnapshot(...args);
  const { key, jobId } = source;
  useEffect(() => {
    onSnapshot(key, snapshot?.jobId === jobId ? snapshot : null);
  }, [key, jobId, onSnapshot, snapshot]);
  useEffect(() => () => onSnapshot(key, null), [key, onSnapshot]);
  return null;
}
