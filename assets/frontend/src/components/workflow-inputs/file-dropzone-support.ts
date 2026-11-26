/** Helpers of the file drop zone, kept apart so that file-dropzone.tsx exports only components. */
import { useRunUploadLimits } from "@/lib/auth/run-upload-limits";
import { useI18n } from "@/lib/i18n/useI18n";

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function sameFile(left: File, right: File): boolean {
  return left.name === right.name && left.size === right.size && left.lastModified === right.lastModified;
}

function megabytes(bytes: number): string {
  const value = bytes / (1024 * 1024);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** The server's limits for a run's files as one line under the drop zone; nothing until they are known. */
export function useUploadLimitsHint(multiple: boolean): string | undefined {
  const { t } = useI18n();
  const limits = useRunUploadLimits();
  if (!limits) return undefined;
  const parts: string[] = [];
  if (limits.max_file_bytes > 0) {
    parts.push(t("react.file_dropzone.limit_size", "Up to {size} MB per file", { size: megabytes(limits.max_file_bytes) }));
  }
  if (multiple && limits.max_files > 0) {
    parts.push(t("react.file_dropzone.limit_count", "up to {count} files", { count: String(limits.max_files) }));
  }
  return parts.length ? parts.join(" · ") : undefined;
}
