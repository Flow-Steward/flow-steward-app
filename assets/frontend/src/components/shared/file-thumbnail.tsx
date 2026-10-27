import { useState } from "react";

import { DsDialog } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";

/** Same-origin image thumbnail and full preview, shared by jobs and workflow notes. */
export function FileThumbnail({ name, previewUrl, description, imageClassName, imageTestId }: Readonly<{
  name: string;
  previewUrl: string;
  description?: string;
  imageClassName?: string;
  imageTestId?: string;
}>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  if (failed) {
    return null;
  }
  return (
    <>
      <button
        aria-label={t("react.jobs.preview_value1", "Preview {value1}", { value1: String(name) })}
        className="shrink-0 overflow-hidden fs-surface-radius border border-subtle"
        onClick={() => setOpen(true)}
        type="button"
      >
        <img
          alt=""
          className={imageClassName ?? "block h-14 w-14 object-cover"}
          data-testid={imageTestId}
          loading="lazy"
          onError={() => setFailed(true)}
          src={previewUrl}
        />
      </button>
      <DsDialog
        description={description ?? ""}
        eyebrow={t("react.jobs.file_preview", "File preview")}
        onClose={() => setOpen(false)}
        open={open}
        title={name}
        variant="catalog"
      >
        <img alt={name} className="mx-auto block max-h-[70vh] max-w-full object-contain" src={previewUrl} />
      </DsDialog>
    </>
  );
}
