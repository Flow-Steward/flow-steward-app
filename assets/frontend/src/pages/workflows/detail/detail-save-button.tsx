import { Check } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";

/** How long ✓ Saved stays inside the button before it rests again. */
export const SAVE_DONE_VISIBLE_MS = 1600;

type SaveButtonPhase = "idle" | "saving" | "done";

/**
 * Save parameters, Save group and Save workflow settings: the button says it,
 * nothing stays on screen. With nothing to save it rests (disabled); a change
 * makes it live; pressed, it reads Saving … (the orbit loader joins after the
 * loader delay), then ✓ Saved — or ✓ Updated when the change joined the
 * unapplied structure — for a moment, and rests again. The three labels share
 * one grid cell, so the button keeps one width. A screen reader hears the result.
 */
export function SaveStateButton({
  id,
  label,
  savingLabel,
  doneLabel,
  doneAnnouncement,
  dirty,
  saving,
  failed = false,
  disabled = false,
  doneSignal,
  onClick,
  testId,
}: Readonly<{
  id?: string;
  label: string;
  savingLabel: string;
  doneLabel: string;
  doneAnnouncement: string;
  /** Something differs from what is saved; false rests the button. */
  dirty: boolean;
  saving: boolean;
  /** The last save did not land: no ✓, the error is said where it happened. */
  failed?: boolean;
  disabled?: boolean;
  /**
   * Bumped by the owner each time a save lands. Without it, the end of a
   * `saving` episode counts as landed unless `failed` says otherwise; a save
   * kept with the unapplied structure has no request, so it needs the signal.
   */
  doneSignal?: number;
  onClick: () => void;
  testId?: string;
}>) {
  const statusId = useId();
  const [done, setDone] = useState(false);
  const wasSaving = useRef(saving);
  const loaderVisible = useLoaderDelay(saving);

  const dirtyWhenDone = useRef(dirty);

  const lastSignal = useRef(doneSignal);
  const [doneEpoch, setDoneEpoch] = useState(0);

  useEffect(() => {
    const finished = wasSaving.current && !saving;
    wasSaving.current = saving;
    if (saving) {
      setDone(false);
      return;
    }
    if (doneSignal === undefined && finished && !failed) setDoneEpoch((epoch) => epoch + 1);
  }, [doneSignal, failed, saving]);

  useEffect(() => {
    if (doneSignal === undefined || doneSignal === lastSignal.current) return;
    lastSignal.current = doneSignal;
    setDoneEpoch((epoch) => epoch + 1);
  }, [doneSignal]);

  useEffect(() => {
    if (!doneEpoch) return;
    // `dirty` is read once, when the save lands; a later change is handled below.
    dirtyWhenDone.current = dirty;
    setDone(true);
    const timer = globalThis.setTimeout(() => setDone(false), SAVE_DONE_VISIBLE_MS);
    return () => globalThis.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doneEpoch]);

  // A new change while ✓ Saved shows makes the button live again at once.
  useEffect(() => {
    if (done && dirty && !dirtyWhenDone.current) setDone(false);
  }, [dirty, done]);

  let phase: SaveButtonPhase = "idle";
  if (saving) phase = "saving";
  else if (done) phase = "done";

  const labels: Array<[SaveButtonPhase, ReactNode]> = [
    ["idle", label],
    ["saving", <>{loaderVisible ? <DsLoader variant="orbit-inline" /> : null}{savingLabel}</>],
    ["done", <><Check aria-hidden="true" />{doneLabel}</>],
  ];

  return (
    <>
      <span className="fs-sr-only" id={statusId} role="status" aria-live="polite">{phase === "done" ? doneAnnouncement : ""}</span>
      <Button
        aria-busy={saving || undefined}
        aria-describedby={statusId}
        className="workflow-save-button"
        data-save-phase={phase}
        data-testid={testId}
        disabled={disabled || saving || phase === "done" || !dirty}
        id={id}
        onClick={onClick}
        type="button"
        variant="primary"
      >
        <span className="workflow-save-labels">
          {labels.map(([key, content]) => (
            <span aria-hidden={key === phase ? undefined : true} data-on={key === phase ? "true" : "false"} key={key}>{content}</span>
          ))}
        </span>
      </Button>
    </>
  );
}
