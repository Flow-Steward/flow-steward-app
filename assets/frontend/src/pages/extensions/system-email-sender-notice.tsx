import { Link, useInRouterContext } from "react-router";

import { useHasCapability } from "@/lib/auth/capabilities";
import { useI18n } from "@/lib/i18n/useI18n";
import "@/pages/extensions/extension-manifest.css";

import { isSystemEmailSender, isSystemEmailSenderLocked } from "./system-email-sender";

const SYSTEM_EMAIL_SETTINGS_PATH = "/settings/email";

function SettingsLink({ label }: Readonly<{ label: string }>) {
  const inRouter = useInRouterContext();
  return inRouter
    ? <Link className="fs-manifest-link" to={SYSTEM_EMAIL_SETTINGS_PATH}>{label}</Link>
    : <a className="fs-manifest-link" href={SYSTEM_EMAIL_SETTINGS_PATH}>{label}</a>;
}

/**
 * Says on a connection that it sends system email and, when it is locked to the
 * viewer, why its controls are read-only. Platform administrators get a link to
 * where the sender is chosen.
 */
export function SystemEmailSenderNotice({ row }: Readonly<{ row: Record<string, unknown> | null | undefined }>) {
  const { t } = useI18n();
  const isAdmin = useHasCapability("administer_platform");
  const sender = isSystemEmailSender(row);
  const locked = isSystemEmailSenderLocked(row);
  if (!sender && !locked) return null;
  return (
    <div className="fs-manifest-notice" data-testid="system-email-sender-notice">
      {sender ? (
        <p>
          {t("react.extensions.system_email_sender_notice", "Used as the system email sender")}
          {isAdmin ? (
            <>
              {" · "}
              <SettingsLink label={t("react.extensions.system_email_sender_settings_link", "Settings › Email")} />
            </>
          ) : null}
        </p>
      ) : null}
      {locked ? (
        <p>{t("react.extensions.system_email_sender_locked", "Only a platform administrator can change the system email sender's connection.")}</p>
      ) : null}
    </div>
  );
}
