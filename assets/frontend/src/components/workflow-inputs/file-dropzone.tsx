import { FileText, Image as ImageIcon, Upload, X } from "lucide-react";
import { useState, type DragEvent } from "react";

import { useRunUploadLimits } from "@/lib/auth/run-upload-limits";
import { useI18n } from "@/lib/i18n/useI18n";

import "./file-dropzone.css";

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

/** The kept end of a long name: its last characters, so the extension stays in view. */
const NAME_TAIL = 12;

/** A name as a head that may shrink with an ellipsis and a tail that never does. */
function nameParts(name: string): { head: string; tail: string } {
  if (name.length <= NAME_TAIL + 6) return { head: name, tail: "" };
  return { head: name.slice(0, name.length - NAME_TAIL), tail: name.slice(name.length - NAME_TAIL) };
}

function draggingFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

/**
 * Files for a form field: a drop zone with a browse link, the limits under it,
 * and the chosen files as a list with their size and a remove button each.
 * Several files add up across picks and drops; a single-file field keeps the
 * last one chosen.
 */
export function FileDropzone({
  files,
  onFiles,
  multiple,
  disabled,
  required,
  inputId,
  name,
  labelId,
  ariaLabel,
  describedBy,
  hint,
  listLabel,
  testId,
}: Readonly<{
  files: readonly File[];
  onFiles: (files: File[]) => void;
  multiple: boolean;
  disabled?: boolean;
  required?: boolean;
  /** The id of the hidden file input, for a label elsewhere that points at it. */
  inputId?: string;
  /** The hidden input's form name. */
  name?: string;
  /** The field's label element; ariaLabel names the input when there is none. */
  labelId?: string;
  ariaLabel?: string;
  describedBy?: string;
  /** What the field accepts: formats and the size limit, one line. */
  hint?: string;
  /** The accessible name of the list of chosen files. */
  listLabel?: string;
  testId?: string;
}>) {
  const { t } = useI18n();
  const [over, setOver] = useState(false);
  const add = (picked: File[]) => {
    if (!picked.length) return;
    onFiles(multiple ? [...files, ...picked.filter((file) => !files.some((known) => sameFile(known, file)))] : picked.slice(0, 1));
  };
  const total = files.reduce((sum, file) => sum + file.size, 0);
  let prompt = multiple ? t("react.file_dropzone.drag_many", "Drag files here or") : t("react.file_dropzone.drag_one", "Drag a file here or");
  if (files.length) {
    prompt = multiple ? t("react.file_dropzone.add_more", "Add more: drag files or") : t("react.file_dropzone.replace", "Replace it: drag a file or");
  }
  if (over) prompt = t("react.file_dropzone.drop", "Drop to add the files");
  return (
    <div className="fs-dropzone-field" data-testid={testId}>
      <div
        className="fs-dropzone"
        data-disabled={disabled ? "true" : undefined}
        data-over={over ? "true" : undefined}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver(false);
        }}
        onDragOver={(event) => {
          if (disabled || !draggingFiles(event)) return;
          event.preventDefault();
          setOver(true);
        }}
        onDrop={(event) => {
          if (disabled) return;
          event.preventDefault();
          setOver(false);
          add(Array.from(event.dataTransfer?.files ?? []));
        }}
      >
        <span aria-hidden="true" className="fs-dropzone-icon">
          <Upload />
        </span>
        <span className="fs-dropzone-text">
          <span>
            {prompt}
            {/* While files hover, the zone says only to drop them; the link stays for the keyboard. */}
            <span hidden={over}> </span>
            <label className="fs-dropzone-pick" data-hidden={over ? "true" : undefined}>
              {t("react.file_dropzone.browse", "browse")}
              <input
                aria-describedby={describedBy}
                aria-label={labelId ? undefined : ariaLabel}
                aria-labelledby={labelId}
                className="fs-sr-only"
                disabled={disabled}
                id={inputId}
                multiple={multiple}
                name={name}
                onChange={(event) => {
                  add(Array.from(event.target.files ?? []));
                  event.target.value = "";
                }}
                required={required && !files.length}
                type="file"
              />
            </label>
          </span>
          {hint ? <span className="fs-dropzone-hint">{hint}</span> : null}
        </span>
      </div>
      {files.length ? (
        <>
          {multiple ? (
            <div className="fs-dropzone-summary">
              <span>
                {t("react.file_dropzone.summary", "{count} · {size}", {
                  count: files.length === 1 ? t("react.file_dropzone.one_file", "1 file") : t("react.file_dropzone.files", "{count} files", { count: String(files.length) }),
                  size: fileSize(total),
                })}
              </span>
              <button className="fs-dropzone-clear" disabled={disabled} onClick={() => onFiles([])} type="button">
                {t("react.file_dropzone.remove_all", "Remove all")}
              </button>
            </div>
          ) : null}
          <ul aria-label={listLabel} className="fs-dropzone-files">
            {files.map((file, index) => {
              const { head, tail } = nameParts(file.name);
              const Icon = file.type.startsWith("image/") ? ImageIcon : FileText;
              return (
                <li className="fs-dropzone-file" key={`${file.name}-${file.size}-${file.lastModified}-${index}`}>
                  <Icon aria-hidden="true" className="fs-dropzone-file-icon" />
                  <span className="fs-dropzone-file-name" title={file.name}>
                    <span className="fs-dropzone-file-head">{head}</span>
                    {tail ? <span className="fs-dropzone-file-tail">{tail}</span> : null}
                  </span>
                  <span className="fs-dropzone-file-size">{fileSize(file.size)}</span>
                  <button
                    aria-label={t("react.operator_form.remove_file", "Remove {name}", { name: file.name })}
                    className="fs-icon-button"
                    disabled={disabled}
                    onClick={() => onFiles(files.filter((_, at) => at !== index))}
                    type="button"
                  >
                    <X aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
    </div>
  );
}
