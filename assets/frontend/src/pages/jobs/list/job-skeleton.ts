/**
 * The first load's stand-in body (skeleton.md: "Fix the count"): five rows,
 * each cell one Skeleton line sized to what its column usually holds. The row
 * actions column is chrome and stays empty.
 */
export const JOB_SKELETON_ROWS = 5;
export const JOB_SKELETON_WIDTHS: Record<string, string> = {
  _workflow_display: "12rem",
  project_name: "6rem",
  status: "4.5rem",
  _started_display: "8rem",
  _completed_display: "8rem",
  _duration: "3rem",
};
