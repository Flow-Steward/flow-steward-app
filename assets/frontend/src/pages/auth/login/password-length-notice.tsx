import { Link, useLocation, useNavigate } from "react-router";

import { DsToast } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";

export const RECOMMENDED_PASSWORD_LENGTH = 12;

export function PasswordLengthNotice() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();

  if (location.state?.showPasswordLengthNotice !== true) return null;

  return (
    <DsToast className="max-w-[calc(100vw-var(--fs-dashboard-section-gap)-var(--fs-dashboard-section-gap))]" tone="neutral">
      <p>{t("react.auth.password_length_recommendation", "Your password is shorter than {count} characters. Choose a longer password.", { count: RECOMMENDED_PASSWORD_LENGTH })}</p>
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
