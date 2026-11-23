import { useContext } from "react";

import type { RunUploadLimits } from "@/lib/auth/AuthProvider";
import { AuthContext } from "@/lib/auth/context";

/** The server's limits for files of a real run; undefined until the session knows them. */
export function useRunUploadLimits(): RunUploadLimits | undefined {
  return useContext(AuthContext)?.session?.run_upload_limits;
}

/**
 * Whether these files break the policy the server enforces. Unknown limits block
 * nothing here: the server still refuses an oversized run with a clear error.
 */
export function exceedsRunUploadLimits(
  sizes: readonly number[],
  limits: RunUploadLimits | undefined,
): boolean {
  if (!limits) return false;
  const total = sizes.reduce((sum, size) => sum + size, 0);
  return (
    (limits.max_files > 0 && sizes.length > limits.max_files) ||
    (limits.max_file_bytes > 0 && sizes.some((size) => size > limits.max_file_bytes)) ||
    (limits.max_total_bytes > 0 && total > limits.max_total_bytes)
  );
}
