import { QRCodeSVG } from "qrcode.react";

import { useI18n } from "@/lib/i18n/useI18n";

export function AuthenticatorQr({ uri }: Readonly<{ uri: string }>) {
  const { t } = useI18n();
  return (
    <QRCodeSVG
      className="block h-auto max-w-full"
      level="M"
      marginSize={4}
      role="img"
      size={224}
      title={t("react.settings.authenticator_setup_qr_code", "Authenticator setup QR code")}
      value={uri}
    />
  );
}
