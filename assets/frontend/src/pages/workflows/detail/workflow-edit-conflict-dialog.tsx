import { useEffect, useRef, useState, type ReactNode } from "react";

import { DsAlert, DsDialog } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { Button } from "@/components/ui/button";
import { isWorkflowRevisionConflict, workflowChangeErrorIssues } from "@/pages/workflows/builder/api";
import { changeIssueMessage, type WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import type { WorkflowEditSessionController } from "@/pages/workflows/detail/use-workflow-edit-session";
import type { WorkflowAuthoringContent } from "@/pages/workflows/detail/workflow-edit-session";
import {
  applyWorkflowEditSession,
  compareWorkflowEditSession,
  type EditSessionComparison,
  type EditSessionCompareResult,
} from "@/pages/workflows/detail/workflow-edit-session-api";
import { useI18n } from "@/lib/i18n/useI18n";

/**
 * The edit session's conflict dialog (Task 6): the author's copy and a newer
 * server version, compared three ways, and an explicit choice.
 *
 * "Keep my version" writes exactly the reviewed copy, guarded by the reviewed
 * revision: a newer revision answers 409 and a new comparison, never a retry.
 * "Accept the server version" loads the server version and starts a new local
 * history; nothing is written. "Decide later" closes the dialog and keeps the
 * copy. A copy that changed while the dialog was open needs a new comparison.
 */

type Review = {
  journal: WorkflowChangeSetController["journal"];
  editSequence: number;
  workflowId: string;
  base: WorkflowAuthoringContent;
  local: WorkflowAuthoringContent;
};
type Stage = "review" | "confirm_mine" | "confirm_server";

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function ComparisonList({ title, comparison, empty }: Readonly<{ title: string; comparison: EditSessionComparison; empty: string }>) {
  const { t } = useI18n();
  return (
    <section aria-label={title} className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {comparison.changes.length ? (
        <div className="fs-changelog__changes">
          {comparison.changes.map((change, index) => (
            <div className="fs-changelog__change" key={`${change.path}-${index}`}>
              <span className="fs-changelog__change-head">
                <span className="fs-changelog__change-path">{change.path}</span>
                <span className="fs-changelog__change-kind">{change.change}</span>
              </span>
              <span className="fs-changelog__change-values">
                {t("project_edit.diff.from", "from:")} {valueText(change.from)} {t("react.registry.to", "• to:")} {valueText(change.to)}
              </span>
            </div>
          ))}
        </div>
      ) : <p className="fs-empty-copy">{empty}</p>}
    </section>
  );
}

/**
 * What the edit session cannot promise: a recovery copy from an earlier visit
 * that cannot be used (kept until the author deletes it), or a browser that
 * cannot keep the copy, so a reload may lose unapplied work.
 */
export function WorkflowEditSessionNotice({ session }: Readonly<{
  session: Pick<WorkflowEditSessionController, "status" | "problem" | "persistence" | "dirty" | "deleteRecovery">;
}>) {
  const { t } = useI18n();
  const reasons = {
    version: t("react.workflows.edit_session_recovery_version", "it was saved by another version of the builder"),
    corrupt: t("react.workflows.edit_session_recovery_corrupt", "it is damaged"),
    identity: t("react.workflows.edit_session_recovery_identity", "it belongs to another user or account"),
    checkpoint: t("react.workflows.edit_session_recovery_checkpoint", "the server version it was made on is missing"),
  } as const;
  const notices: ReactNode[] = [];
  if (session.status === "recovery_unavailable") {
    notices.push(
      <DsAlert key="recovery" tone="warning" title={t("react.workflows.edit_session_recovery_unavailable", "The local copy from an earlier visit cannot be restored: {reason}. It is kept until you delete it; nothing was applied.", {
        reason: session.problem ? reasons[session.problem] : reasons.corrupt,
      })}>
        <Button onClick={() => void session.deleteRecovery()} size="sm" type="button" variant="secondary">
          {t("react.workflows.edit_session_delete_recovery", "Delete the local copy")}
        </Button>
      </DsAlert>,
    );
  }
  if (session.persistence === "failed" || (session.persistence === "unavailable" && session.dirty)) {
    notices.push(
      <p className="px-4 py-1 text-xs text-[var(--fs-status-attention)]" data-testid="edit-session-storage-notice" key="storage">
        {t("react.workflows.edit_session_not_kept", "This browser could not keep a recovery copy. A reload may lose your unapplied work: apply it, or keep this tab open.")}
      </p>,
    );
  }
  return notices.length ? <>{notices}</> : null;
}

export function WorkflowEditConflictDialog({
  open,
  changeSet,
  session,
  refetchWorkflowSurfaces,
  onClose,
}: Readonly<{
  open: boolean;
  changeSet: WorkflowChangeSetController;
  session: Pick<WorkflowEditSessionController, "baseContent" | "localContent" | "editSequence">;
  refetchWorkflowSurfaces: () => Promise<unknown>;
  onClose: () => void;
}>) {
  const { t } = useI18n();
  const [stage, setStage] = useState<Stage>("review");
  const [review, setReview] = useState<Review | null>(null);
  const [result, setResult] = useState<EditSessionCompareResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requestRef = useRef(0);
  const changeSetRef = useRef(changeSet);
  const sessionRef = useRef(session);
  useEffect(() => {
    changeSetRef.current = changeSet;
    sessionRef.current = session;
  });

  const compare = async (message = "") => {
    const base = sessionRef.current.baseContent();
    const local = sessionRef.current.localContent();
    const current = changeSetRef.current;
    const request = ++requestRef.current;
    setStage("review");
    setResult(null);
    setError("");
    setNotice(message);
    if (!base || !local) {
      setReview(null);
      setError(t("react.workflows.edit_session_compare_unavailable", "The versions cannot be compared now. Your copy is kept; nothing was written."));
      return;
    }
    const captured: Review = { journal: current.journal, editSequence: sessionRef.current.editSequence(), workflowId: current.workflowId, base, local };
    setReview(captured);
    setLoading(true);
    try {
      const compared = await compareWorkflowEditSession(captured.workflowId, { base_content: base, local_content: local });
      if (request !== requestRef.current) return;
      setResult(compared);
    } catch (error_) {
      if (request !== requestRef.current) return;
      setError(error_ instanceof Error && error_.message
        ? t("react.workflows.edit_session_compare_failed", "The versions could not be compared: {reason} Your copy is kept; nothing was written.", { reason: error_.message })
        : t("react.workflows.edit_session_compare_unavailable", "The versions cannot be compared now. Your copy is kept; nothing was written."));
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (!open) {
      ++requestRef.current;
      setStage("review");
      setReview(null);
      setResult(null);
      setError("");
      setNotice("");
      return;
    }
    void compare();
    // One comparison per opening; later ones are the author's or a 409's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  /** The review still describes the copy on screen: same owner, same history, no later action. */
  const reviewIsCurrent = (captured: Review) => {
    const current = changeSetRef.current;
    return current.workflowId === captured.workflowId && current.journal === captured.journal &&
      sessionRef.current.editSequence() === captured.editSequence;
  };

  const keepMine = async () => {
    if (!review || !result || busy) return;
    if (!reviewIsCurrent(review)) {
      await compare(t("react.workflows.edit_session_review_stale", "Your copy changed after the comparison. Review the new comparison, then choose again."));
      return;
    }
    setBusy(true);
    setError("");
    const request = requestRef.current;
    try {
      const saved = await applyWorkflowEditSession(review.workflowId, { content: review.local, expected_revision: result.current_revision });
      if (request !== requestRef.current || changeSetRef.current.workflowId !== review.workflowId) return;
      changeSetRef.current.keepOwnVersionApplied(saved.revision);
      await refetchWorkflowSurfaces();
      onClose();
    } catch (error_) {
      if (request !== requestRef.current) return;
      if (isWorkflowRevisionConflict(error_)) {
        // Someone saved again: nothing was written; the author reviews the newer version.
        setBusy(false);
        await compare(t("react.workflows.edit_session_newer_revision", "Another newer version was saved meanwhile. Nothing was written. Review the new comparison, then choose again."));
        return;
      }
      const issues = workflowChangeErrorIssues(error_).map((issue) => changeIssueMessage(issue, t)).filter(Boolean);
      setStage("review");
      setError(issues.length
        ? t("react.workflows.edit_session_mine_refused", "Your version was not saved: {reason}", { reason: issues.join(" ") })
        : t("react.workflows.edit_session_mine_failed", "Your version was not saved. Your copy is kept; nothing was written."));
    } finally {
      if (request === requestRef.current) setBusy(false);
    }
  };

  const acceptServer = async () => {
    if (busy) return;
    const owner = changeSetRef.current.workflowId;
    setBusy(true);
    setError("");
    const request = requestRef.current;
    try {
      const refreshed = await refetchWorkflowSurfaces() as { workflow_id?: unknown; revision?: unknown } | null;
      if (request !== requestRef.current || changeSetRef.current.workflowId !== owner) return;
      const revision = Number(refreshed?.revision);
      if (refreshed?.workflow_id !== owner || !Number.isSafeInteger(revision) || revision <= 0) {
        setStage("review");
        setError(t("react.workflows.edit_session_server_unavailable", "The server version could not be loaded. Your copy is kept."));
        return;
      }
      changeSetRef.current.acceptServerVersion();
      onClose();
    } catch {
      if (request !== requestRef.current) return;
      setStage("review");
      setError(t("react.workflows.edit_session_server_unavailable", "The server version could not be loaded. Your copy is kept."));
    } finally {
      if (request === requestRef.current) setBusy(false);
    }
  };

  const revision = result?.current_revision;
  let footer;
  if (stage === "confirm_mine") {
    footer = <>
      <Button disabled={busy} onClick={() => setStage("review")} type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
      <Button disabled={busy} onClick={() => void keepMine()} type="button">{t("react.workflows.edit_session_confirm_mine", "Replace with my version")}</Button>
    </>;
  } else if (stage === "confirm_server") {
    footer = <>
      <Button disabled={busy} onClick={() => setStage("review")} type="button" variant="secondary">{t("common.cancel", "Cancel")}</Button>
      <Button disabled={busy} onClick={() => void acceptServer()} type="button">{t("react.workflows.edit_session_confirm_server", "Discard my copy")}</Button>
    </>;
  } else {
    footer = <>
      <Button disabled={busy} onClick={onClose} type="button" variant="secondary">{t("react.workflows.edit_session_decide_later", "Decide later")}</Button>
      <Button disabled={busy} onClick={() => setStage("confirm_server")} type="button" variant="secondary">{t("react.workflows.edit_session_accept_server", "Accept the server version")}</Button>
      <Button disabled={busy || loading || !result} onClick={() => setStage("confirm_mine")} type="button">{t("react.workflows.edit_session_keep_mine", "Keep my version")}</Button>
    </>;
  }

  return (
    <DsDialog
      busy={busy}
      description={t("react.workflows.edit_session_conflict_description", "Your copy was made on revision {base}. The server now has revision {current}. Nothing is merged automatically.", {
        base: changeSet.checkpoint.revision ?? "—",
        current: revision ?? "—",
      })}
      eyebrow={t("react.workflows.edit_session_eyebrow", "Workflow changed elsewhere")}
      footer={footer}
      onClose={onClose}
      onEscape={() => (stage === "review" ? onClose() : setStage("review"))}
      open={open}
      title={t("react.workflows.edit_session_conflict_title", "Compare your copy with the server")}
      variant="form"
    >
      <div className="space-y-4">
        {notice ? <DsAlert title={notice} /> : null}
        {error ? <DsAlert title={error} tone="danger" /> : null}
        {loading ? <span className="fs-changelog__loading"><DsLoader variant="orbit-inline" />{t("react.workflows.edit_session_comparing", "Comparing versions…")}</span> : null}
        {stage === "confirm_mine" && revision ? (
          <DsAlert tone="warning" title={t("react.workflows.edit_session_confirm_mine_text", "Save your copy as the version after revision {revision}? Changes on the server that your copy does not have will be replaced. Whether the workflow is on, its notes and input secrets stay as they are.", { revision })} />
        ) : null}
        {stage === "confirm_server" ? (
          <DsAlert tone="warning" title={t("react.workflows.edit_session_confirm_server_text", "Discard your copy and its Undo history, and continue from the server version? Nothing is written to the server.")} />
        ) : null}
        {result ? <>
          <ComparisonList comparison={result.base_to_local} empty={t("react.workflows.edit_session_no_changes", "No changes.")} title={t("react.workflows.edit_session_my_changes", "My changes")} />
          <ComparisonList comparison={result.base_to_server} empty={t("react.workflows.edit_session_no_changes", "No changes.")} title={t("react.workflows.edit_session_server_changes", "Changes on the server")} />
          <ComparisonList comparison={result.local_to_server} empty={t("react.workflows.edit_session_no_changes", "No changes.")} title={t("react.workflows.edit_session_differences", "My copy compared with the server")} />
        </> : null}
      </div>
    </DsDialog>
  );
}
