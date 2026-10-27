import { useLayoutEffect, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { objectOrEmpty, testRunPayloadFromInputs } from "./detail-helpers";
import { workflowTestRunInputRow } from "./detail-test-run-controller";
import { readWorkflowRunFile, runBuilderWorkflow } from "../shared/run-api";

type RunOptions = {
  workflowId: string; projectId: string; revision: number | null; enabled: boolean; canRun: boolean;
  blockedReason: string; contract: unknown; contractReady: boolean; summary: string;
  inputs: Record<string, string>; fieldErrors: Record<string, string>; testBusy: boolean;
  open: boolean; launchLock: { current: symbol | null };
};

/** Ordinary jobs never enter the test controller, history, samples or result cache. */
export function useWorkflowNormalRunController(options: RunOptions) {
  const { t } = useI18n();
  const [acknowledged, setAcknowledged] = useState(false);
  const [files, setFiles] = useState<Record<string, File>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [jobId, setJobId] = useState("");
  const generation = useRef(0);
  const mounted = useRef(false);
  const owner = JSON.stringify([options.workflowId, options.projectId, options.revision, options.open]);
  const committed = useRef(options);
  const contract = objectOrEmpty(objectOrEmpty(options.contract).interface);
  const fields = Array.isArray(contract.expected_inputs) ? contract.expected_inputs.map(objectOrEmpty) : [];
  const fileFields = fields.filter((field) => field.type === "file");
  const totalBytes = Object.values(files).reduce((size, file) => size + file.size, 0);
  const blocked = !options.canRun || !options.enabled || !options.projectId || !Number.isSafeInteger(options.revision) || Number(options.revision) < 1 ||
    contract.enabled !== true || !options.contractReady || Boolean(options.blockedReason) || !options.summary.trim() || options.testBusy ||
    !Array.isArray(contract.trigger_channels) || !contract.trigger_channels.includes("ui") ||
    Object.keys(options.fieldErrors).some((path) => !fileFields.some((field) => path === `inputs.${String(field.name)}`)) ||
    fileFields.some((field) => field.required && !files[String(field.name)]) ||
    Object.values(files).some((file) => !file.size) || Object.keys(files).length > 10 || totalBytes > 1_048_576;
  const live = useRef({ blocked, acknowledged, files, owner });
  useLayoutEffect(() => { committed.current = options; live.current = { blocked, acknowledged, files, owner }; });
  useLayoutEffect(() => {
    generation.current += 1;
    setAcknowledged(false); setFiles({}); setMessage(""); setJobId("");
  }, [owner]);
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; generation.current += 1; }; }, []);
  async function start() {
    const current = live.current;
    const draft = committed.current;
    if (!mounted.current || current.blocked || !current.acknowledged || draft.launchLock.current) return;
    const token = Symbol("ordinary-workflow-run");
    const capturedGeneration = generation.current;
    const isCurrent = () => mounted.current && generation.current === capturedGeneration && live.current.owner === current.owner;
    draft.launchLock.current = token;
    setBusy(true); setMessage("");
    try {
      const uploads = await Promise.all(Object.entries(current.files).map(([name, file]) => readWorkflowRunFile(name, file)));
      if (!isCurrent() || live.current.blocked) return;
      const interfaceFields = objectOrEmpty(objectOrEmpty(draft.contract).interface).expected_inputs;
      const rows = (Array.isArray(interfaceFields) ? interfaceFields : []).map(objectOrEmpty)
        .map((field) => workflowTestRunInputRow(field, t)).filter((row) => row.type !== "file" && !row.fixed);
      const response = await runBuilderWorkflow(draft.workflowId, {
        project_id: draft.projectId, expected_revision: draft.revision!, acknowledged: true,
        summary: draft.summary.trim(), inputs: testRunPayloadFromInputs(rows, draft.inputs), files: uploads,
      }, crypto.randomUUID());
      if (!isCurrent()) return;
      setJobId(response.job_id); setAcknowledged(false); setFiles({});
    } catch (error) {
      if (isCurrent()) setMessage(error instanceof Error && error.message ? error.message : t("react.workflows.normal_run_failed", "Workflow could not start."));
    } finally {
      if (draft.launchLock.current === token) draft.launchLock.current = null;
      if (mounted.current) setBusy(false);
    }
  }
  return { acknowledged, setAcknowledged, busy, message, jobId, blocked, start, fileFields, files, fileOwner: owner,
    setFile: (name: string, file: File | undefined) => setFiles((previous) => {
      const next = { ...previous }; if (file) next[name] = file; else delete next[name]; return next;
    }) };
}
export type WorkflowNormalRunController = ReturnType<typeof useWorkflowNormalRunController>;
