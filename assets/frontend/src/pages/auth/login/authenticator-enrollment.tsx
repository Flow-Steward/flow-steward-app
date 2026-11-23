import { AuthenticatorQr } from "@/components/shared/authenticator-qr";
import { useI18n } from "@/lib/i18n/useI18n";

export type AuthenticatorEnrollment = { token: string; secret: string; uri: string };

export default function LoginAuthenticatorEnrollment({ enrollment }: Readonly<{ enrollment: AuthenticatorEnrollment }>) {
  const { t } = useI18n();
  return (
    <div className="fs-public-auth__field">
      <p>{t("react.settings.authenticator_scan_instructions", "Scan the QR code with your authenticator app or use the setup key below.")}</p>
      <AuthenticatorQr uri={enrollment.uri} />
      <code className="break-all">{enrollment.secret}</code>
      <a className="fs-public-auth__quiet-link" href={enrollment.uri}>
        {t("react.settings.authenticator_open", "Open in an authenticator app")}
      </a>
    </div>
  );
}
