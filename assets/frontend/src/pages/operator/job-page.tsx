import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useMemo } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { filePreviewKind, filePreviewUrl, previewTooLarge } from "@/components/file-preview/file-preview-model";
import { DsAlert, DsPageHeader, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { useFilePreviewMaxBytes } from "@/lib/auth/file-preview-limit";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { todayIn } from "@/lib/time/calendar-days";
import { useUserTimezone } from "@/lib/time/user-timezone";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { JobsDetailPage } from "@/pages/jobs/detail";
import { fetchJobsQueuePosition, type JobsQueuePosition } from "@/pages/jobs/list/api";

import { fetchJobReview, fetchOperatorSection } from "./api";
import { isTypingTarget, operatorJobPath, queueListFilters, sectionListPath } from "./queue";
import { OperatorJobView } from "./operator-job-view";
import { OperatorReviewPanel } from "./review-panel";
import { reviewFileUrl, reviewPageDecides, type JobReview } from "./review-model";
import "./operator.css";

const prefetched = new Set<string>();

/** Load the first original of the next job ahead, so it shows at once; never one over the preview limit. */
function prefetchOriginal(review: JobReview | undefined, maxBytes: number | undefined) {
  const gate = review?.gate;
  const file = gate?.review?.original?.[0];
  if (!gate || !file?.artifact_id || !filePreviewKind(file) || previewTooLarge(file, maxBytes)) return;
  const url = filePreviewUrl(reviewFileUrl(gate.job_id, file));
  if (!url || prefetched.has(url) || typeof document === "undefined") return;
  prefetched.add(url);
  const link = document.createElement("link");
  link.rel = "prefetch";
  link.href = url;
  document.head.appendChild(link);
}

function QueueBar({
  position,
  listPath,
  sectionTitle,
  onPrevious,
  onNext,
}: Readonly<{
  position: JobsQueuePosition | undefined;
  listPath: string;
  sectionTitle: string;
  onPrevious: (() => void) | null;
  onNext: (() => void) | null;
}>) {
  const { t } = useI18n();
  let where = "";
  if (position?.position) {
    where = t("react.operator_queue.position", "{position} of {total}", { position: String(position.position), total: String(position.total) });
  } else if (position) {
    where = t("react.operator_queue.left_list", "Not in the list any more · {total} left", { total: String(position.total) });
  }
  return (
    <nav aria-label={t("react.operator_queue.label", "Queue")} className="fs-operator-queue" data-testid="operator-queue">
      <Link className="fs-link fs-operator-queue-back" to={listPath}>
        <ArrowLeft aria-hidden="true" />
        {t("react.operator_queue.back", "Back to {title}", { title: sectionTitle })}
      </Link>
      <div className="fs-operator-queue-steps">
        <button
          aria-keyshortcuts="K"
          aria-label={t("react.operator_queue.previous", "Previous job (K)")}
          className="fs-icon-button"
          disabled={!onPrevious}
          onClick={() => onPrevious?.()}
          type="button"
        >
          <ChevronLeft aria-hidden="true" />
        </button>
        <span aria-live="polite" data-testid="operator-queue-position">
          {where}
        </span>
        <button
          aria-keyshortcuts="J"
          aria-label={t("react.operator_queue.next", "Next job (J)")}
          className="fs-icon-button"
          disabled={!onNext}
          onClick={() => onNext?.()}
          type="button"
        >
          <ChevronRight aria-hidden="true" />
        </button>
      </div>
    </nav>
  );
}

/**
 * A job of a section (ADR 0075), opened from the section's list with that list
 * in its address: "3 of 47", the previous and next job (K / J), and a way back
 * to the same list. A job waiting for a review with fields (its own, or one
 * down its Invoke chain) shows the review page; any other job the operator
 * view: submitted values, the configured result and the decided review.
 * Without list context the queue is the section's default list.
 */
export function OperatorJobPage() {
  const { t } = useI18n();
  const { projectId = "", workflowId = "", jobId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const previewMaxBytes = useFilePreviewMaxBytes();
  const timezone = useUserTimezone();
  const section = useQuery({
    queryKey: queryKeys.operatorSection(projectId, workflowId),
    queryFn: () => fetchOperatorSection(projectId, workflowId),
    enabled: Boolean(projectId && workflowId),
    retry: false,
  });
  const review = useQuery({
    queryKey: queryKeys.operatorJobReview(jobId),
    queryFn: () => fetchJobReview(jobId, projectId, workflowId),
    enabled: Boolean(jobId),
    retry: false,
  });
  const listFilters = useMemo(
    () =>
      section.data
        ? queueListFilters(projectId, workflowId, section.data.operator.filters ?? [], searchParams, todayIn(timezone))
        : null,
    [projectId, workflowId, section.data, searchParams, timezone],
  );
  const listKey = listFilters ? JSON.stringify(listFilters) : "";
  const position = useQuery({
    queryKey: queryKeys.operatorQueuePosition(jobId, listKey),
    queryFn: () => (listFilters ? fetchJobsQueuePosition(listFilters, jobId) : Promise.reject(new Error("No list"))),
    enabled: Boolean(listFilters && jobId),
    retry: false,
  });
  const listPath = sectionListPath(projectId, workflowId, searchParams);
  const open = useCallback(
    (target: string | null | undefined) => {
      ignoreAsyncResult(navigate(target ? operatorJobPath(projectId, workflowId, target, searchParams) : listPath));
    },
    [navigate, projectId, workflowId, searchParams, listPath],
  );
  const previousId = position.data?.previous_job_id ?? null;
  const nextId = position.data?.next_job_id ?? null;
  /** The next job even when the position has not arrived yet: it is asked for, then opened. */
  const openNext = () => {
    if (position.data || !listFilters) {
      open(nextId);
      return;
    }
    ignoreAsyncResult(
      queryClient
        .fetchQuery({ queryKey: queryKeys.operatorQueuePosition(jobId, listKey), queryFn: () => fetchJobsQueuePosition(listFilters, jobId) })
        .then((found) => open(found.next_job_id), () => open(null)),
    );
  };

  // The next job and its first original load ahead.
  useEffect(() => {
    if (!nextId) return;
    ignoreAsyncResult(
      queryClient
        .prefetchQuery({ queryKey: queryKeys.operatorJobReview(nextId), queryFn: () => fetchJobReview(nextId, projectId, workflowId), retry: false })
        .then(() => prefetchOriginal(queryClient.getQueryData<JobReview>(queryKeys.operatorJobReview(nextId)), previewMaxBytes)),
    );
  }, [nextId, queryClient, projectId, workflowId, previewMaxBytes]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || isTypingTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === "j" && nextId) {
        event.preventDefault();
        open(nextId);
      } else if (key === "k" && previousId) {
        event.preventDefault();
        open(previousId);
      }
    };
    globalThis.addEventListener("keydown", onKey);
    return () => globalThis.removeEventListener("keydown", onKey);
  }, [nextId, previousId, open]);

  const refreshAfterDecision = () => {
    void queryClient.invalidateQueries({ queryKey: ["operator", "review", jobId] });
    void queryClient.invalidateQueries({ queryKey: ["operator", "queue"] });
  };

  if (section.isLoading || review.isLoading) {
    return <PageLoadingState label={t("react.operator_queue.loading", "Opening the job")} />;
  }
  if (!section.data) {
    return (
      <PageContainer width="wide">
        <DsAlert title={t("react.operator.not_found", "This section is not available.")} tone="warning" />
      </PageContainer>
    );
  }
  const data = review.data;
  const bar = (
    <QueueBar
      listPath={listPath}
      onNext={nextId ? () => open(nextId) : null}
      onPrevious={previousId ? () => open(previousId) : null}
      position={position.data}
      sectionTitle={section.data.title}
    />
  );
  if (data?.gate?.review && (data.access === "decide" || data.access === "read") && !reviewPageDecides(data.gate)) {
    // Override, abort or a recovery gate: the existing review panel of the job that holds it.
    return (
      <>
        <PageContainer width="standard">{bar}</PageContainer>
        <JobsDetailPage jobId={data.gate.job_id} />
      </>
    );
  }
  if (data?.gate?.review && (data.access === "decide" || data.access === "read")) {
    return (
      <PageContainer width="wide">
        {bar}
        <DsPageHeader
          description={t("react.operator_review.description", "Compare the fields with the original, correct what is wrong and decide.")}
          eyebrow={section.data.title}
          title={t("react.operator_review.title", "Review")}
        />
        <OperatorReviewPanel
          key={`${data.gate.job_id}:${data.gate.approval_id ?? ""}`}
          onApprovedNext={() => {
            // The neighbours are by sort key: the one asked before the decision is still next.
            openNext();
            refreshAfterDecision();
          }}
          onDecided={refreshAfterDecision}
          onSkip={openNext}
          review={data}
        />
      </PageContainer>
    );
  }
  // Any other job, running, finished or waiting where the caller cannot decide:
  // the operator view, never the technical step graph.
  return (
    <PageContainer width="standard">
      {bar}
      <OperatorJobView jobId={jobId} review={data} section={section.data} />
    </PageContainer>
  );
}
