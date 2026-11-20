import { Link, useLocation, useNavigate } from "react-router";

import { DsToast } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { PASSWORD_MIN_LENGTH } from "@/lib/auth/password-policy";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";

export function PasswordLengthNotice() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();

  const days: unknown = location.state?.passwordExpiresInDays;
  const nearExpiry = typeof days === "number" && Number.isInteger(days) && days > 0 && days <= 14;
  const shortPassword = location.state?.showPasswordLengthNotice === true;
  if (!nearExpiry && !shortPassword) return null;

  const expiryWarning = days === 1
    ? t("react.auth.password_expiry_warning_one", "Your password expires in 1 day. Change it before it expires.")
    : t("react.auth.password_expiry_warning", "Your password expires in {count} days. Change it before it expires.", { count: nearExpiry ? days : 0 });

  return (
    <DsToast className="max-w-[calc(100vw-var(--fs-dashboard-section-gap)-var(--fs-dashboard-section-gap))]" tone="neutral">
      {nearExpiry ? <p>{expiryWarning}</p> : null}
      {shortPassword ? <p>{t("react.auth.password_length_recommendation", "Your password is shorter than {count} characters. Choose a longer password.", { count: PASSWORD_MIN_LENGTH })}</p> : null}
      <div className="mt-2 flex flex-wrap gap-2">
        <Button asChild size="sm" variant="secondary">
          <Link to="/settings/account?tab=signin">{t("react.settings.change_password", "Change password")}</Link>
        </Button>
        <Button
          onClick={() => { ignoreAsyncResult(navigate(location, { replace: true, state: null })); }}
          size="sm"
          type="button"
          variant="ghost"
        >
          {t("common.close", "Close")}
        </Button>
      </div>
    </DsToast>
  );
}
