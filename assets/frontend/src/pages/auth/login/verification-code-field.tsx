import type { UseFormRegisterReturn } from "react-hook-form";

import { useI18n } from "@/lib/i18n/useI18n";

export function LoginMfaField({ error, registration }: Readonly<{
  error?: string;
  registration: UseFormRegisterReturn<"mfaCode">;
}>) {
  const { t } = useI18n();

  return (
    <div className="fs-public-auth__field">
      <label className="fs-public-auth__field-label" htmlFor="login-verification-code">
        {t("react.auth.mfa_code", "Authenticator or recovery code")}
      </label>
      <input
        {...registration}
        aria-describedby="login-verification-helper"
        aria-invalid={Boolean(error)}
        autoCapitalize="none"
        autoComplete="one-time-code"
        className="fs-public-auth__input"
        id="login-verification-code"
        spellCheck={false}
        type="password"
      />
      <span className={error ? "fs-public-auth__field-helper fs-public-auth__field-helper--error" : "fs-public-auth__field-helper"} id="login-verification-helper">
        {error || t("react.auth.mfa_code_required", "Enter a code from your authenticator app or a recovery code.")}
      </span>
    </div>
  );
}
