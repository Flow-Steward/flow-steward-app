import { useI18n } from "@/lib/i18n/useI18n";
import { Upload } from "lucide-react";
import { useCallback, useEffect, useId, useState, type ChangeEvent, type SyntheticEvent } from "react";

import { BlankCell, DsAlert, DsBadge, DsCard } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { fileSize } from "@/components/workflow-inputs/file-dropzone-support";
import { fetchJson } from "@/lib/http/client";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { isFeatureNotEntitled } from "@/pages/settings/hub/resource-report";
import { RegisteredPageUnavailable } from "@/pages/settings/hub/resource-report-panel";
import {
  documentByteLength,
  statusObject,
  statusValueText,
  type StatusDocumentField,
  type StatusDocumentSurface,
} from "@/pages/settings/hub/status-document";

/**
 * Host renderer for `control_surface.kind = "status_document"` (ADR 0082, section 6):
 * the module's status as label and value rows, and a text document the person
 * pastes or reads from a file, sent to the module with PUT. The page's
 * `required_permission` decides who reaches it; the module checks the write again.
 */

function StatusValue({ field, status, timezone }: Readonly<{
  field: StatusDocumentField;
  status: Record<string, unknown>;
  timezone: string;
}>) {
  const raw = status[field.key];
  const text = statusValueText(raw);
  if (!text) return <BlankCell />;
  if (field.type === "badge") return <DsBadge>{text}</DsBadge>;
  if (field.type === "datetime") return <>{formatUserDateTime(raw, timezone) || text}</>;
  return <>{text}</>;
}

export function StatusDocumentPanel({
  surface,
  title,
}: Readonly<{
  surface: StatusDocumentSurface;
  title: string;
}>) {
  const { t } = useI18n();
  const timezone = useUserTimezone();
  const documentId = useId();
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  /** The server's reason when the account may not use this page; null while it may. */
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const [documentText, setDocumentText] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [uploaded, setUploaded] = useState(false);
  const tooLarge = t("react.settings.status_document_too_large", "The document is larger than {limit}.", { limit: fileSize(surface.maxBytes) });

  const load = useCallback(
    async (isCurrent: () => boolean) => {
      setLoading(true);
      setMessage("");
      try {
        const payload = await fetchJson<unknown>(surface.statusPath);
        if (!isCurrent()) return;
        setStatus(statusObject(surface, payload));
        setUnavailable(null);
      } catch (error) {
        if (!isCurrent()) return;
        setStatus(null);
        if (isFeatureNotEntitled(error)) {
          setUnavailable(error instanceof Error ? error.message : "");
        } else {
          setMessage(error instanceof Error ? error.message : t("react.settings.failed_to_load_this_settings_area", "Failed to load this settings area."));
        }
      } finally {
        if (isCurrent()) setLoading(false);
      }
    },
    [surface, t],
  );

  useEffect(() => {
    let current = true;
    ignoreAsyncResult(load(() => current));
    return () => {
      current = false;
    };
  }, [load, reloads]);

  const loaderShown = useLoaderDelay(loading && !status);

  async function readFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    setUploaded(false);
    if (file.size > surface.maxBytes) {
      setUploadError(tooLarge);
      return;
    }
    try {
      setDocumentText(await file.text());
      setUploadError("");
    } catch {
      setUploadError(t("react.settings.status_document_read_failed", "The file could not be read."));
    }
  }

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setUploaded(false);
    if (!documentText.trim()) {
      setUploadError(t("react.settings.status_document_empty", "Paste the document or choose a file first."));
      return;
    }
    if (documentByteLength(documentText) > surface.maxBytes) {
      setUploadError(tooLarge);
      return;
    }
    setUploading(true);
    setUploadError("");
    try {
      await fetchJson<unknown>(surface.uploadPath, {
        method: "PUT",
        body: JSON.stringify({ [surface.uploadField]: documentText }),
      });
      setDocumentText("");
      setUploaded(true);
      setReloads((value) => value + 1);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : t("react.settings.status_document_upload_failed", "The document was not accepted."));
    } finally {
      setUploading(false);
    }
  }

  if (unavailable !== null) {
    return <RegisteredPageUnavailable description={unavailable} />;
  }

  let statusBody = null;
  if (loaderShown) {
    statusBody = <div className="fs-table-state"><DsLoader variant="wide-block" /></div>;
  } else if (status) {
    statusBody = (
      <dl className="fs-form-grid">
        {surface.fields.map((field) => (
          <div className="fs-field" key={field.key}>
            <dt className="fs-field-label">{field.label}</dt>
            <dd><StatusValue field={field} status={status} timezone={timezone} /></dd>
          </div>
        ))}
      </dl>
    );
  } else if (!loading && !message) {
    statusBody = <p className="fs-ink-secondary">{surface.emptyText || t("react.settings.status_document_no_status", "No status is available yet.")}</p>;
  }

  const uploadLabel = surface.uploadLabel || t("react.settings.status_document_document", "Document");
  return (
    <>
      <DsCard description={surface.description || undefined} title={title}>
        <div className="fs-panel-heading">
          <span />
          <Button busy={loading} busyLabel={t("registry.index.reloading", "Reloading…")} onClick={() => setReloads((value) => value + 1)} type="button" variant="secondary">
            {t("account.tokens.refresh", "Refresh")} </Button>
        </div>
        {message ? <DsAlert title={message} tone="warning" /> : null}
        {statusBody}
      </DsCard>
      <DsCard description={surface.uploadDescription || undefined} title={uploadLabel}>
        <form className="fs-panel-group" noValidate onSubmit={(event) => ignoreAsyncResult(submit(event))}>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor={documentId}>{uploadLabel}</label>
            <textarea
              className="fs-textarea fs-code-value"
              id={documentId}
              name={surface.uploadField}
              onChange={(event) => {
                setDocumentText(event.target.value);
                setUploaded(false);
              }}
              spellCheck={false}
              value={documentText}
            />
          </div>
          {uploadError ? <DsAlert title={uploadError} tone="danger" /> : null}
          {uploaded ? <DsAlert title={t("react.settings.status_document_saved", "Document uploaded.")} tone="success" /> : null}
          <div className="fs-footer-actions">
            <label className="fs-button cursor-pointer">
              <Upload aria-hidden="true" />
              <span>{t("react.settings.status_document_choose_file", "Choose file")}</span>
              <input
                accept={surface.accept || undefined}
                className="sr-only"
                onChange={(event) => ignoreAsyncResult(readFile(event))}
                type="file"
              />
            </label>
            <Button busy={uploading} busyLabel={t("react.settings.status_document_uploading", "Uploading…")} type="submit" variant="primary">
              {t("react.settings.status_document_upload", "Upload")}
            </Button>
          </div>
        </form>
      </DsCard>
    </>
  );
}
