import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { WorkflowCanvasGraph, WorkflowCanvasIssue } from "@/pages/workflows/shared/api";

type Owner = { key: string };
type Selection = { owner: Owner; issue: WorkflowCanvasIssue } | null;
type ValidationSource = {
  workflowId: string; projectId: string; revision: number | null;
  graph: WorkflowCanvasGraph | undefined; isSuccess: boolean; isFetching: boolean;
};

function issueKey(issue: WorkflowCanvasIssue): string {
  return JSON.stringify([issue.scope, issue.scope_id, issue.authored_id, issue.step_id,
    issue.group_id, issue.code || issue.title_key || issue.title, issue.field_path]);
}

export function useFocusedValidationIssue(source: ValidationSource) {
  const key = JSON.stringify([source.workflowId, source.projectId]);
  const [owner, setOwner] = useState<Owner>({ key });
  if (owner.key !== key) setOwner({ key });
  const committedOwner = useRef<Owner | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  useLayoutEffect(() => {
    committedOwner.current = owner;
    return () => { if (committedOwner.current === owner) committedOwner.current = null; };
  }, [owner]);
  const setSelectedCanvasIssue = useCallback((issue: WorkflowCanvasIssue | null) => {
    if (committedOwner.current !== owner) return;
    setSelection((current) => committedOwner.current === owner ? issue ? { owner, issue } : null : current);
  }, [owner]);
  useLayoutEffect(() => {
    // Only current saved validation can retire the clicked issue; a Save ACK,
    // failed refresh or older retained query data is not that proof.
    const graph = source.graph;
    if (!source.isSuccess || source.isFetching || graph?.workflow.id !== source.workflowId ||
      source.revision === null || graph.workflow.revision !== source.revision) return;
    setSelection((current) => {
      if (!current || current.owner !== owner || committedOwner.current !== owner) return current;
      const fresh = graph.validation_issues.find((issue) => issueKey(issue) === issueKey(current.issue));
      return fresh ? fresh === current.issue ? current : { owner, issue: fresh } : null;
    });
  }, [owner, selection, source.graph, source.isSuccess, source.isFetching, source.revision, source.workflowId]);
  return { selectedCanvasIssue: owner.key === key && selection?.owner === owner ? selection.issue : null,
    setSelectedCanvasIssue };
}
